"""GPIF 写回工具：读入/替换 .gp/.gpx 里的 ``Content/score.gpif``。

只做文件层操作（保留 zip 元数据、恢复 GP8 要求的 CDATA 写法），
不包含任何音乐逻辑。gpchords 的和弦/调性写回都建立在它之上。
"""

from __future__ import annotations

import html
import io
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path


_CDATA_PAIR_RE = re.compile(
    r"<(\w+)>(\s*)<!\[CDATA\[(.*?)\]\]>(\s*)</\1>", re.S
)


def cdata_pairs_from(xml_text: str) -> list[tuple[str, str, str, str]]:
    """收集原文件里用 CDATA 包裹过的 (标签, 文本, 前空白, 后空白)。

    GP8 原生文件的 CDATA 通常前后带换行（``<Letter>\\n<![CDATA[A]]>\\n</Letter>``），
    旧正则要求紧凑写法，漏掉了所有多行 CDATA，导致写回后段落标记等文本
    退化为普通文本、GP8 静默丢弃。
    """
    return [
        (m.group(1), m.group(3), m.group(2), m.group(4))
        for m in _CDATA_PAIR_RE.finditer(xml_text)
    ]


def restore_cdata(xml_text: str, pairs: list[tuple[str, str, str, str]]) -> str:
    """
    把 ET 序列化时丢失的 CDATA 包回对应标签。

    GP8 的 GPIFReader 只认 CDATA 形式的文本（实测拍上的 <Chord> 引用若写成
    普通文本，GP8 会静默丢弃所有和弦标注）。ET 不会输出 CDATA，因此在
    序列化完成后按原文件的 (标签, 文本) 对逐个恢复；另外把拍上的
    <Chord> 数字引用全部恢复为 CDATA（新增的和弦也适用）。
    """
    # 空 CDATA 的元素（如 <SubTitle><![CDATA[]]></SubTitle>）
    empty_tags = {tag for tag, value, _, _ in pairs if value == ""}
    for tag in empty_tags:
        xml_text = re.sub(
            rf"<{tag}>\s*</{tag}>",
            f"<{tag}><![CDATA[]]></{tag}>",
            xml_text,
        )
        xml_text = re.sub(
            rf"<{tag} />",
            f"<{tag}><![CDATA[]]></{tag}>",
            xml_text,
        )
    # 按 (标签, 文本) 精确匹配：ET 序列化时文本里的 & < > 已转义
    for tag, value, ws_left, ws_right in pairs:
        if value == "":
            continue
        xml_text = re.sub(
            rf"<{tag}>\s*{re.escape(html.escape(value, quote=False))}\s*</{tag}>",
            f"<{tag}>{ws_left}<![CDATA[{value}]]>{ws_right}</{tag}>",
            xml_text,
        )
    # 拍上的和弦引用：<Chord>CDATA[i]</Chord>
    xml_text = re.sub(
        r"<Chord>(\d+)</Chord>",
        r"<Chord><![CDATA[\1]]></Chord>",
        xml_text,
    )
    # 拍上的自由文本注解（罗马数字等）：新增的 <FreeText> 也必须是 CDATA，
    # 否则 GP8 静默丢弃；原文件已有的 CDATA 形式不会被这里二次包裹
    xml_text = re.sub(
        r"<FreeText>([^<]*?)</FreeText>",
        r"<FreeText><![CDATA[\1]]></FreeText>",
        xml_text,
    )
    return xml_text


def restore_section_cdata(xml_text: str) -> str:
    """把新增段落标记（<Section>）里的 Letter/Text 包成 CDATA。

    GP8 的 GPIFReader 只认 CDATA 形式的段落文本（实测），而 ET 序列化
    不产生 CDATA；只处理 Section 内部的 Letter/Text，避免误伤
    <Lyrics><Line><Text> 等其他同名标签。
    """

    def wrap(tag: str, inner: str) -> str:
        inner = inner.strip()
        if inner and inner.startswith("<![CDATA[") and inner.endswith("]]>"):
            wrapped = inner
        else:
            wrapped = f"<![CDATA[{inner}]]>"
        return f"<{tag}>{wrapped}</{tag}>"

    def fix_section(match) -> str:
        body = match.group(1)
        body = re.sub(
            r"<(Letter|Text)>(.*?)</\1>",
            lambda m: wrap(m.group(1), m.group(2)),
            body,
            flags=re.S,
        )
        return "<Section>" + body + "</Section>"

    return re.sub(
        r"<Section>(.*?)</Section>", fix_section, xml_text, flags=re.S
    )


def read_gpif(input_path: str | Path) -> tuple[ET.Element, str]:
    """读入原始 GPIF XML 树，返回 (根元素, 文件名)。"""
    with zipfile.ZipFile(input_path) as zin:
        names = zin.namelist()
        gpif_name = (
            "Content/score.gpif" if "Content/score.gpif" in names else "score.gpif"
        )
        xml_bytes = zin.read(gpif_name)
    return ET.fromstring(xml_bytes), gpif_name


def _write_zip_with_gpif(
    input_path: str | Path,
    output_path: str | Path,
    gpif_bytes: bytes,
) -> None:
    """把替换后的 score.gpif 写回新的 .gp/.gpx 文件（原文件不动）。

    逐项保留原 zip 的压缩方式、时间戳与 extra 属性——GP8 对 zip 容器结构敏感。
    除 score.gpif 外所有条目原样复制，不经过 XML 重序列化。
    """
    with zipfile.ZipFile(input_path) as zin:
        infos = zin.infolist()
        names = set(zin.namelist())
        gpif_name = (
            "Content/score.gpif" if "Content/score.gpif" in names else "score.gpif"
        )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zout:
            for info in infos:
                # 只读当前条目，避免把整个 zip 解压到内存。
                content = (
                    gpif_bytes
                    if info.filename == gpif_name
                    else zin.read(info.filename)
                )
                new_info = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                new_info.compress_type = info.compress_type
                new_info.external_attr = info.external_attr
                new_info.internal_attr = info.internal_attr
                new_info.comment = info.comment
                new_info.create_system = info.create_system
                new_info.create_version = info.create_version
                new_info.extract_version = info.extract_version
                new_info.reserved = info.reserved
                new_info.flag_bits = info.flag_bits
                if info.extra:
                    new_info.extra = info.extra
                zout.writestr(new_info, content)
    with open(output_path, "wb") as f:
        f.write(buffer.getvalue())


def rewrite_gpif_text(
    input_path: str | Path,
    output_path: str | Path,
    transform,
) -> None:
    """对 score.gpif 的**文本**应用 ``transform(str) -> str`` 后写回新文件。

    与 :func:`write_gpif` 不同，这里不做 XML 重序列化，只按原文本做
    定向替换（如只改 tempo automation 的 <Value>），其余内容逐字节保留，
    风险最小。其余 zip 条目原样复制。
    """
    with zipfile.ZipFile(input_path) as zin:
        names = set(zin.namelist())
        gpif_name = (
            "Content/score.gpif" if "Content/score.gpif" in names else "score.gpif"
        )
        xml_text = zin.read(gpif_name).decode("utf-8")
    _write_zip_with_gpif(input_path, output_path, transform(xml_text).encode("utf-8"))


def write_gpif(
    input_path: str | Path,
    output_path: str | Path,
    root: ET.Element,
    extra_fix=None,
) -> None:
    """把修改后的 GPIF XML 树写回新的 .gp/.gpx 文件（原文件不动）。

    逐项保留原 zip 的压缩方式、时间戳与 extra 属性——GP8 对 zip 容器结构敏感。
    ``extra_fix``：可选回调，在 CDATA 恢复后、写盘前对 XML 文本做追加处理
    （如 gpchords 的段落写回需要把新增 <Section> 的 Letter/Text 包成 CDATA）。
    """
    with zipfile.ZipFile(input_path) as zin:
        names = set(zin.namelist())
        gpif_name = (
            "Content/score.gpif" if "Content/score.gpif" in names else "score.gpif"
        )
        gpif_text = zin.read(gpif_name).decode("utf-8")
        xml_text = ET.tostring(root, encoding="unicode")
        xml_text = restore_cdata(xml_text, cdata_pairs_from(gpif_text))
        if extra_fix is not None:
            xml_text = extra_fix(xml_text)
        xml_bytes = (
            '<?xml version="1.0" encoding="utf-8"?>\n'.encode("utf-8")
            + xml_text.encode("utf-8")
        )
    _write_zip_with_gpif(input_path, output_path, xml_bytes)
