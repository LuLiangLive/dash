"""
security/sanitizer.py —— HTML 消毒模块（XSS 防护）

用于对需要使用 v-html 渲染的 HTML 内容进行消毒，
移除危险的标签和属性（script、onerror、javascript: 等）。

使用 Python 标准库 html.parser 实现，无需额外依赖。
允许的标签和属性采用白名单机制。
"""
from __future__ import annotations

import html
import logging
import re
from html.parser import HTMLParser

logger = logging.getLogger("security.sanitizer")

# 允许的 HTML 标签白名单
ALLOWED_TAGS = {
    "a", "abbr", "address", "article", "aside", "b", "blockquote", "br",
    "caption", "cite", "code", "col", "colgroup", "dd", "del", "details",
    "dfn", "div", "dl", "dt", "em", "figcaption", "figure", "footer",
    "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "i", "img",
    "ins", "kbd", "li", "main", "mark", "nav", "ol", "p", "pre", "q",
    "rp", "rt", "ruby", "s", "samp", "section", "small", "span", "strong",
    "sub", "summary", "sup", "table", "tbody", "td", "tfoot", "th",
    "thead", "time", "tr", "u", "ul", "var", "wbr",
}

# 允许的属性白名单（按标签分类）
ALLOWED_ATTRS = {
    "*": {"class", "id", "title", "style", "data-*", "role", "aria-*"},
    "a": {"href", "target", "rel", "hreflang", "download"},
    "img": {"src", "alt", "width", "height", "loading", "srcset"},
    "td": {"colspan", "rowspan", "headers", "scope"},
    "th": {"colspan", "rowspan", "headers", "scope", "abbr"},
    "time": {"datetime"},
    "ol": {"start", "reversed", "type"},
    "li": {"value"},
    "col": {"span", "width"},
    "colgroup": {"span", "width"},
    "table": {"summary"},
}

# 危险的 URL 协议（会被移除）
DANGEROUS_PROTOCOLS = {
    "javascript:", "vbscript:", "data:text/html", "data:text/javascript",
    "blob:", "file:",
}

# 事件处理器属性模式（on*）
EVENT_HANDLER_PATTERN = re.compile(r"^on[a-z]+$", re.IGNORECASE)

# CSS expression 模式（IE 旧版 XSS）
CSS_EXPRESSION_PATTERN = re.compile(r"expression\s*\(", re.IGNORECASE)


def _is_data_attr(attr: str) -> bool:
    """检查是否为 data-* 属性。"""
    return attr.startswith("data-")


def _is_aria_attr(attr: str) -> bool:
    """检查是否为 aria-* 属性。"""
    return attr.startswith("aria-")


def _attr_allowed(tag: str, attr: str) -> bool:
    """检查属性是否在白名单中。"""
    attr_lower = attr.lower()

    # 全局属性
    global_attrs = ALLOWED_ATTRS.get("*", set())
    if attr_lower in global_attrs:
        return True
    if _is_data_attr(attr_lower) and "data-*" in global_attrs:
        return True
    if _is_aria_attr(attr_lower) and "aria-*" in global_attrs:
        return True

    # 标签特定属性
    tag_attrs = ALLOWED_ATTRS.get(tag.lower(), set())
    if attr_lower in tag_attrs:
        return True

    return False


def _sanitize_url(url: str) -> str:
    """消毒 URL，移除危险协议。"""
    if not url:
        return url
    url_lower = url.strip().lower()
    for proto in DANGEROUS_PROTOCOLS:
        if url_lower.startswith(proto):
            return ""
    # 检查编码的 javascript:
    if re.match(r"^[\s\u0000-\u0020]*j[\s\u0000-\u0020]*a", url_lower):
        return ""
    return url


def _sanitize_style(style: str) -> str:
    """消毒 style 属性，移除 expression 等危险内容。"""
    if not style:
        return style
    if CSS_EXPRESSION_PATTERN.search(style):
        return ""
    # 移除 url() 中的 javascript:
    style = re.sub(
        r"url\s*\(\s*['\"]?\s*javascript:[^)]*\)",
        "",
        style,
        flags=re.IGNORECASE,
    )
    return style


class _HTMLSanitizer(HTMLParser):
    """HTML 消毒解析器。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.output = []
        self._skip_stack = []

    def handle_starttag(self, tag, attrs):
        tag_lower = tag.lower()

        if tag_lower not in ALLOWED_TAGS:
            # 跳过危险标签，但记录层级以便正确处理结束标签
            self._skip_stack.append(tag_lower)
            return

        # 构建消毒后的属性
        safe_attrs = []
        for attr, value in attrs:
            attr_lower = attr.lower()

            # 移除事件处理器
            if EVENT_HANDLER_PATTERN.match(attr_lower):
                continue

            if not _attr_allowed(tag_lower, attr):
                continue

            if value is None:
                safe_attrs.append(attr_lower)
                continue

            # 消毒 URL 属性
            if attr_lower in ("href", "src", "action", "formaction"):
                value = _sanitize_url(value)
                if not value:
                    continue

            # 消毒 style
            if attr_lower == "style":
                value = _sanitize_style(value)
                if not value:
                    continue

            # 对 a 标签强制添加 rel
            if tag_lower == "a" and attr_lower == "target" and value == "_blank":
                # 确保有 rel=noopener
                pass

            safe_attrs.append(f'{attr_lower}="{html.escape(value, quote=True)}"')

        # 对 a[target=_blank] 强制添加 rel=noopener noreferrer
        if tag_lower == "a":
            has_target_blank = any(
                a[0].lower() == "target" and a[1] == "_blank" for a in attrs
            )
            has_rel = any(a[0].lower() == "rel" for a in attrs)
            if has_target_blank and not has_rel:
                safe_attrs.append('rel="noopener noreferrer"')

        attr_str = " ".join(safe_attrs)
        if attr_str:
            self.output.append(f"<{tag_lower} {attr_str}>")
        else:
            self.output.append(f"<{tag_lower}>")

    def handle_endtag(self, tag):
        tag_lower = tag.lower()

        # 如果在跳过栈中，弹出并不输出
        if self._skip_stack and self._skip_stack[-1] == tag_lower:
            self._skip_stack.pop()
            return

        if tag_lower in ALLOWED_TAGS:
            self.output.append(f"</{tag_lower}>")

    def handle_startendtag(self, tag, attrs):
        tag_lower = tag.lower()
        if tag_lower not in ALLOWED_TAGS:
            return

        safe_attrs = []
        for attr, value in attrs:
            attr_lower = attr.lower()
            if EVENT_HANDLER_PATTERN.match(attr_lower):
                continue
            if not _attr_allowed(tag_lower, attr):
                continue
            if value is None:
                safe_attrs.append(attr_lower)
                continue
            if attr_lower in ("href", "src"):
                value = _sanitize_url(value)
                if not value:
                    continue
            if attr_lower == "style":
                value = _sanitize_style(value)
                if not value:
                    continue
            safe_attrs.append(f'{attr_lower}="{html.escape(value, quote=True)}"')

        attr_str = " ".join(safe_attrs)
        if attr_str:
            self.output.append(f"<{tag_lower} {attr_str} />")
        else:
            self.output.append(f"<{tag_lower} />")

    def handle_data(self, data):
        # 跳过危险标签内的文本内容（如 script、style）
        if self._skip_stack:
            return
        self.output.append(data)

    def handle_comment(self, data):
        # 移除 HTML 注释（可能包含条件注释漏洞）
        pass

    def handle_decl(self, decl):
        # 移除 DOCTYPE 声明
        pass

    def handle_pi(self, data):
        # 移除处理指令
        pass

    def get_output(self) -> str:
        return "".join(self.output)


def sanitize_html(html_content: str) -> str:
    """
    消毒 HTML 内容，移除危险标签和属性。

    Args:
        html_content: 原始 HTML 字符串

    Returns:
        消毒后的安全 HTML 字符串
    """
    if not html_content:
        return html_content

    try:
        sanitizer = _HTMLSanitizer()
        sanitizer.feed(html_content)
        sanitizer.close()
        return sanitizer.get_output()
    except Exception as e:
        logger.error("HTML 消毒失败: %s，返回转义后的纯文本", e)
        # 降级：全部转义为纯文本
        return html.escape(html_content)


def sanitize_text(text: str) -> str:
    """
    对纯文本进行 HTML 转义（用于不需要 HTML 的场景）。
    """
    if not text:
        return text
    return html.escape(text, quote=True)


def get_sanitizer_config() -> dict:
    """获取消毒器配置（用于诊断）。"""
    return {
        "allowed_tags_count": len(ALLOWED_TAGS),
        "allowed_tags": sorted(ALLOWED_TAGS),
        "dangerous_protocols": sorted(DANGEROUS_PROTOCOLS),
        "removes": ["script", "style", "iframe", "object", "embed", "form", "input",
                    "button", "select", "textarea", "link", "meta", "base", "svg",
                    "math", "event_handlers", "javascript_urls", "css_expressions"],
    }
