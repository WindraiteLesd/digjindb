"""不依赖第三方库从图片字节里读出尺寸.

为什么需要: 比较"两张封面哪张质量更高"最直接的指标就是**分辨率**。项目是纯 stdlib,
所以这里直接解析文件头 —— 只读几个字节, 不需要解码像素。

支持 JPEG / PNG / GIF / WebP(BMP 顺手也支持了)。
"""

from __future__ import annotations

import struct
from typing import Optional

Size = tuple[int, int]


def probe_image_size(blob: bytes) -> Optional[Size]:
    """返回 ``(宽, 高)``; 认不出来返回 ``None``(调用方当作"质量未知")."""
    if not blob or len(blob) < 16:
        return None
    try:
        if blob[:8] == b"\x89PNG\r\n\x1a\n":
            return _png_size(blob)
        if blob[:3] == b"\xff\xd8\xff":
            return _jpeg_size(blob)
        if blob[:6] in (b"GIF87a", b"GIF89a"):
            return _gif_size(blob)
        if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
            return _webp_size(blob)
        if blob[:2] == b"BM":
            return _bmp_size(blob)
    except (struct.error, IndexError, ValueError):
        return None
    return None


def _png_size(blob: bytes) -> Optional[Size]:
    # IHDR 紧跟在 8 字节签名 + 4 字节长度 + 4 字节类型之后
    width, height = struct.unpack(">II", blob[16:24])
    return (int(width), int(height)) or None


def _gif_size(blob: bytes) -> Optional[Size]:
    width, height = struct.unpack("<HH", blob[6:10])
    return (int(width), int(height)) or None


def _bmp_size(blob: bytes) -> Optional[Size]:
    width, height = struct.unpack("<ii", blob[18:26])
    return (abs(int(width)), abs(int(height))) or None


#: JPEG 里带尺寸的 SOF 标记(排除 DHT/JPG/DAC 那几个不带的)
_JPEG_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
             0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def _jpeg_size(blob: bytes) -> Optional[Size]:
    index = 2
    total = len(blob)
    while index + 3 < total:
        if blob[index] != 0xFF:
            index += 1
            continue
        marker = blob[index + 1]
        # 填充字节 0xFF 可以重复出现
        while marker == 0xFF and index + 2 < total:
            index += 1
            marker = blob[index + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        if index + 4 > total:
            break
        length = struct.unpack(">H", blob[index + 2:index + 4])[0]
        if length < 2:
            break
        if marker in _JPEG_SOF:
            if index + 9 > total:
                return None
            height, width = struct.unpack(">HH", blob[index + 5:index + 9])
            return (int(width), int(height)) or None
        index += 2 + length
    return None


def _webp_size(blob: bytes) -> Optional[Size]:
    chunk = blob[12:16]
    if chunk == b"VP8 ":
        if len(blob) < 30:
            return None
        width, height = struct.unpack("<HH", blob[26:30])
        return (int(width) & 0x3FFF, int(height) & 0x3FFF)
    if chunk == b"VP8L":
        if len(blob) < 25:
            return None
        bits = int.from_bytes(blob[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return (width, height)
    if chunk == b"VP8X":
        if len(blob) < 30:
            return None
        width = int.from_bytes(blob[24:27], "little") + 1
        height = int.from_bytes(blob[27:30], "little") + 1
        return (width, height)
    return None


def pixel_area(size: Optional[Size]) -> int:
    if not size:
        return 0
    return int(size[0]) * int(size[1])


def better_cover(ours: Optional[Size], theirs: Optional[Size], *,
                 ours_bytes: int = 0, theirs_bytes: int = 0) -> bool:
    """网易云那张是否**不比库里差**(值得替换).

    规则:

    * 两边都能读出尺寸 -> 比较像素面积(相同则比字节数, 大者胜)
    * 只有一边读得出 -> 读得出的那张更可信, 用它
    * 都读不出 -> 按字节数, 都没有信息就**让网易云赢**
      (用户明确要求"不同则以网易云为准")
    """
    ours_area, theirs_area = pixel_area(ours), pixel_area(theirs)
    if ours and theirs:
        if theirs_area != ours_area:
            return theirs_area > ours_area
        return int(theirs_bytes) >= int(ours_bytes)
    if theirs and not ours:
        return True
    if ours and not theirs:
        return False
    return True
