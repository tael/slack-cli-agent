"""학습 제안 — 생성, 표시, 반영, 되돌리기.

원본 bot.py 의 show_proposal()/apply_learning()/revert_learning() 과
learn.py 의 분석·반영·되돌리기 로직을 이식한다.
"""

from .analyzer import ChannelAnalysisResult, ProposalAnalyzer, ProposalBuilder
from .apply import LearningApplier, LearningReverter
from .batch import BatchReport, LearningBatch
from .proposal import LearningProposal, ProposalStore
from .reactions import ReactionCollector
from .render import ProposalRenderer
from .schedule import DailyBatchSchedule
from .service import LearningService

__all__ = [
    "BatchReport",
    "ChannelAnalysisResult",
    "DailyBatchSchedule",
    "LearningApplier",
    "LearningBatch",
    "LearningProposal",
    "LearningReverter",
    "LearningService",
    "ProposalAnalyzer",
    "ProposalBuilder",
    "ProposalRenderer",
    "ProposalStore",
    "ReactionCollector",
]
