"""
点击验证码 LLM 解算器

使用视觉大模型识别腾讯图形或文字点选验证码，按参考目标顺序返回点击坐标。

策略:
1. 图形题下载参考图标条并拆分；文字题直接读取 DOM 中的有序目标
2. 下载主图并以 data URI 发送（确保 LLM 能访问）
3. 文字题使用多次识别一致性和字形中心校正，图形题保持单次识别
"""

import base64
import io
import json
import logging
import math
import os
import re
import statistics
from itertools import permutations
from typing import List, Optional, Tuple

import requests
from PIL import Image
from openai import OpenAI

from . import const

logger = logging.getLogger(__name__)

_TEXT_REFERENCE_SAMPLE_COUNT = 3
_TEXT_REFERENCE_CONSENSUS_RATIO = 0.06


class ClickCaptchaSolver:
    """基于大模型的点击验证码解算器。"""

    def __init__(self,
                 api_key: Optional[str] = None,
                 model: Optional[str] = None,
                 base_url: Optional[str] = None):
        self.api_key = api_key or const.LLM_API_KEY
        self.model = model or const.LLM_MODEL
        self.base_url = base_url or const.LLM_BASE_URL
        self._client: Optional[OpenAI] = None

    @property
    def client(self) -> OpenAI:
        if self._client is None:
            if not self.api_key:
                raise RuntimeError("LLM_API_KEY 未设置，验证码解算将失败")
            self._client = OpenAI(base_url=self.base_url, api_key=self.api_key)
        return self._client

    def solve(self, ref_url: Optional[str], main_url: str,
              main_width: int, main_height: int,
              reference_text: Optional[str] = None) -> List[Tuple[int, int]]:
        """准备图形或文字参考目标，单次调用 LLM 返回有序坐标。"""
        # 1. 准备图形或文字参考目标
        targets = self._normalize_reference_text(reference_text)
        icon_uris = []
        if not targets:
            ref_raw = self._download(ref_url)
            if not ref_raw:
                return []
            icon_uris = self._split_strip(ref_raw)
            if len(icon_uris) < 3:
                return []

        # 2. 下载主图并转为 data URI（确保LLM能访问）
        main_raw = self._download(main_url)
        if not main_raw:
            return []
        main_uri = "data:image/png;base64," + base64.b64encode(main_raw).decode("ascii")

        # 3. 图形题单次识别；文字题用三次结果做一致性裁决，降低偶发误点风险
        if targets:
            samples = []
            for sample_index in range(_TEXT_REFERENCE_SAMPLE_COUNT):
                sample = self._find_all_icons(
                    icon_uris,
                    main_uri,
                    main_width,
                    main_height,
                    reference_text=targets,
                )
                if len(sample) >= len(targets):
                    samples.append(sample[:len(targets)])
                logger.info(
                    "文字点选识别样本 %s/%s: 坐标数=%s",
                    sample_index + 1,
                    _TEXT_REFERENCE_SAMPLE_COUNT,
                    len(sample),
                )
            coords = self._select_consensus_coordinates(
                samples,
                main_width,
                main_height,
            )
            coords = self._refine_text_coordinates(main_raw, coords)
        else:
            coords = self._find_all_icons(
                icon_uris,
                main_uri,
                main_width,
                main_height,
            )
        if len(coords) < 2:
            return []

        # 钳制到主图范围内
        return [
            (max(0, min(x, main_width - 1)), max(0, min(y, main_height - 1)))
            for x, y in coords
        ]

    @staticmethod
    def _normalize_reference_text(reference_text: Optional[str]) -> List[str]:
        if not reference_text:
            return []
        return re.findall(r"[\u3400-\u9fff]|[A-Za-z0-9]+", reference_text)[:3]

    @staticmethod
    def _select_consensus_coordinates(
        samples: List[List[Tuple[int, int]]],
        main_width: int,
        main_height: int,
    ) -> List[Tuple[int, int]]:
        """选择至少两组相互接近的结果，并按目标顺序取中位数。"""
        if len(samples) < 2:
            logger.warning("文字点选有效识别样本不足，取消本次点击")
            return []

        tolerance = max(
            12.0,
            max(main_width, main_height) * _TEXT_REFERENCE_CONSENSUS_RATIO,
        )
        agreeing_groups = []
        for index, sample in enumerate(samples):
            group = [sample]
            for other_index, other in enumerate(samples):
                if other_index == index or len(other) != len(sample):
                    continue
                distances = [
                    math.dist((x1, y1), (x2, y2))
                    for (x1, y1), (x2, y2) in zip(sample, other)
                ]
                if distances and max(distances) <= tolerance:
                    group.append(other)
            if len(group) >= 2:
                agreeing_groups.append(group)

        if not agreeing_groups:
            logger.warning("文字点选多次识别结果无一致结论，取消本次点击")
            return []

        group = max(agreeing_groups, key=len)
        result = []
        for target_index in range(len(group[0])):
            xs = [sample[target_index][0] for sample in group]
            ys = [sample[target_index][1] for sample in group]
            result.append((
                round(statistics.median(xs)),
                round(statistics.median(ys)),
            ))
        logger.info(
            "文字点选一致性通过: 样本=%s/%s, 容差=%.1fpx",
            len(group),
            len(samples),
            tolerance,
        )
        return result

    @staticmethod
    def _refine_text_coordinates(
        main_raw: bytes,
        coords: List[Tuple[int, int]],
    ) -> List[Tuple[int, int]]:
        """把模型粗坐标吸附到腾讯文字点选题的黄/橙色字形中心。"""
        if not coords:
            return []
        try:
            image = Image.open(io.BytesIO(main_raw)).convert("HSV")
        except Exception:
            return coords

        width, height = image.size
        raw_mask = set()
        pixels = image.load()
        for y in range(height):
            for x in range(width):
                hue, saturation, value = pixels[x, y]
                if 8 <= hue <= 48 and saturation >= 110 and value >= 150:
                    raw_mask.add((x, y))
        if not raw_mask:
            return coords

        # 轻微膨胀，把同一汉字中相邻但不接触的笔画连成一个组件。
        dilated = set(raw_mask)
        for x, y in tuple(raw_mask):
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < width and 0 <= ny < height:
                        dilated.add((nx, ny))

        components = []
        while dilated:
            seed = dilated.pop()
            queue = [seed]
            expanded = [seed]
            for x, y in queue:
                for neighbor in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if neighbor in dilated:
                        dilated.remove(neighbor)
                        queue.append(neighbor)
                        expanded.append(neighbor)

            points = [point for point in expanded if point in raw_mask]
            if len(points) < 30:
                continue
            xs = [point[0] for point in points]
            ys = [point[1] for point in points]
            min_x, max_x = min(xs), max(xs)
            min_y, max_y = min(ys), max(ys)
            if max_x - min_x < 10 or max_y - min_y < 10:
                continue
            components.append((
                round((min_x + max_x) / 2),
                round((min_y + max_y) / 2),
            ))

        max_distance = max(28.0, max(width, height) * 0.16)
        candidates = [
            center
            for center in components
            if any(math.dist(center, coord) <= max_distance for coord in coords)
        ]
        if len(candidates) < len(coords):
            return coords

        best_assignment = None
        best_score = float("inf")
        for assignment in permutations(candidates, len(coords)):
            distances = [
                math.dist(coord, center)
                for coord, center in zip(coords, assignment)
            ]
            if max(distances) > max_distance:
                continue
            score = sum(distances)
            if score < best_score:
                best_score = score
                best_assignment = assignment

        if best_assignment is None:
            return coords
        refined = list(best_assignment)
        logger.info(
            "文字点选字形中心校正: 最大移动=%.1fpx",
            max(math.dist(before, after) for before, after in zip(coords, refined)),
        )
        return refined

    def _download(self, url: Optional[str]) -> Optional[bytes]:
        """下载图片，支持 http 和 data URI。"""
        if not url:
            return None
        try:
            if url.startswith("data:"):
                _, encoded = url.split(",", 1)
                try:
                    return base64.b64decode(encoded)
                except Exception:
                    return base64.b64decode(__import__('urllib.parse').unquote(encoded))
            resp = requests.get(url, timeout=15)
            if resp.status_code == 200:
                return resp.content
            logger.error(f"下载失败: HTTP {resp.status_code}")
            return None
        except Exception as e:
            logger.error(f"下载错误: {e}")
            return None

    def _split_strip(self, raw: bytes) -> List[str]:
        """将参考图标条三等分为独立图标的 data URI。"""
        try:
            img = Image.open(io.BytesIO(raw))
            w, h = img.size
            logger.info(f"参考图标条: {w}x{h}")

            part_w = w // 3
            uris = []
            for i in range(3):
                left = i * part_w
                right = (i + 1) * part_w if i < 2 else w
                icon = img.crop((left, 0, right, h))
                # 放大图标以便LLM看清细节
                icon = icon.resize((icon.width * 3, icon.height * 3), Image.LANCZOS)
                buf = io.BytesIO()
                icon.save(buf, format="PNG")
                uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
                uris.append(uri)
                logger.info(f"图标 #{i + 1}: {icon.width}x{icon.height}")
            return uris
        except Exception as e:
            logger.error(f"拆分错误: {e}")
            return []

    def _find_all_icons(self, icon_uris: List[str], main_uri: str,
                        main_width: int, main_height: int,
                        reference_text: Optional[List[str]] = None) -> List[Tuple[int, int]]:
        """单次 API 调用，让 LLM 找到所有有序目标。"""
        content = []
        if reference_text:
            ordered = "、".join(f"“{item}”" for item in reference_text)
            prompt = (
                f"大图（{main_width}×{main_height}像素）是一张文字点选验证码。\n"
                f"请严格按顺序找到文字：{ordered}。\n"
                "文字可能使用艺术字体、旋转、扭曲、缩放或颜色干扰。\n\n"
                '输出JSON：{"coords":[[x1,y1],[x2,y2],[x3,y3]]}\n'
                "其中x、y为每个目标文字中心的比例坐标（0~1）。"
            )
        else:
            prompt = (
                f"大图（{main_width}×{main_height}像素）是一个图标网格。\n"
                "找到3个参考图标(A, B, C)各自在大图网格中的位置。\n"
                "匹配规则：形状和颜色必须一致，空心/实心、线条粗细是关键区分点，允许旋转。\n\n"
                '输出JSON：{"coords":[[xA,yA],[xB,yB],[xC,yC]]}\n'
                "其中x、y为图标中心的比例坐标（0~1）。"
            )
            labels = ["A", "B", "C"]
            for i, uri in enumerate(icon_uris[:3]):
                content.append({"type": "image_url", "image_url": {"url": uri}})
                content.append({"type": "text", "text": f"参考图标{labels[i]}"})

        content.append({"type": "image_url", "image_url": {"url": main_uri}})
        content.append({"type": "text", "text": prompt})

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "Output valid JSON only. No markdown, no explanation."},
                    {"role": "user", "content": content},
                ],
                max_tokens=4096,
                response_format={"type": "json_object"},
            )
            output = response.choices[0].message.content or ""
            logger.info(f"大模型响应: {output[:400]}")
            return self._parse_coordinates(output, main_width, main_height)
        except Exception as e:
            logger.error(f"大模型错误: {e}")
            return []

    def _parse_coordinates(self, text: str,
                           main_width: int, main_height: int) -> List[Tuple[int, int]]:
        """从LLM返回文本中提取JSON坐标并转为像素。"""
        # 优先尝试JSON解析
        match = re.search(r'\{.*"coords"\s*:\s*\[.*?\]\s*\}', text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
                result = []
                for x, y in data["coords"]:
                    x, y = float(x), float(y)
                    if max(x, y) <= 1.5:
                        result.append((round(x * main_width), round(y * main_height)))
                    else:
                        result.append((round(x), round(y)))
                return result
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                pass

        # 回退：正则解析
        coords = []
        paren_pairs = re.findall(r'\(\s*(\d+\.?\d*)\s*[,，]\s*(\d+\.?\d*)\s*\)', text)
        for x_str, y_str in paren_pairs:
            x, y = float(x_str), float(y_str)
            coords.append((x, y))

        if not coords:
            # 宽松匹配
            nums = re.findall(r'(\d+\.?\d+)', text)
            for i in range(0, len(nums) - 1, 2):
                coords.append((float(nums[i]), float(nums[i + 1])))

        result = []
        for x, y in coords[:3]:
            max_val = max(x, y)
            if max_val <= 1.5:
                result.append((round(x * main_width), round(y * main_height)))
            else:
                result.append((round(x), round(y)))
        return result
