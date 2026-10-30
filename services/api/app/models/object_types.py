"""The kinds of object a lineage link can point at, by the name links store."""

from app.models.analysis_object import AnalysisObject
from app.models.entity import Entity
from app.models.episode_object import EpisodeObject
from app.models.evidence_object import EvidenceObject
from app.models.memory import Memory
from app.models.observation import Observation
from app.models.pattern import Pattern
from app.models.session_transcript import SessionTranscript

OBJECT_MODELS = {
    "evidence": EvidenceObject,
    "episode": EpisodeObject,
    "analysis": AnalysisObject,
    "memory": Memory,
    "entity": Entity,
    "observation": Observation,
    "pattern": Pattern,
    "transcript": SessionTranscript,
}
