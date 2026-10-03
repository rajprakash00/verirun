"""The tool layer: a uniform Tool interface, browser and file tools, and the
Task Pack allowlist with its policy gate."""

from company_operator.tools.base import Tool, ToolError
from company_operator.tools.browser import BrowserSession, build_browser_tools
from company_operator.tools.erp import build_erp_tools
from company_operator.tools.files import build_file_tools
from company_operator.tools.gate import PolicyGate
from company_operator.tools.mail import build_mail_tools
from company_operator.tools.registry import ToolRegistry

__all__ = [
    "BrowserSession",
    "PolicyGate",
    "Tool",
    "ToolError",
    "ToolRegistry",
    "build_browser_tools",
    "build_erp_tools",
    "build_file_tools",
    "build_mail_tools",
]
