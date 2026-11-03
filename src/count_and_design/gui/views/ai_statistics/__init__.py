"""AI statistics workflow views."""

from .pages import (
    ElementRecognitionPage,
    InfoRecognitionPage,
    PageClassificationPage,
    ValidationPage,
)
from .tab import AIStatisticsTab
from .workflow_bar import WorkflowBar

__all__ = [
    "AIStatisticsTab",
    "ElementRecognitionPage",
    "InfoRecognitionPage",
    "PageClassificationPage",
    "ValidationPage",
    "WorkflowBar",
]
