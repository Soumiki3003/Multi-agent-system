import logging
import re
from pathlib import Path


class CTFReferenceService:
    """Ground-truth angr_ctf level guide, used to sanity-check what the
    knowledge graph retrieves before the tutor answers a student.

    The KG (`chunks.json`) only encodes a handful of concepts and is missing
    entire levels (11-17), so it can return partial or stale information for
    a given challenge. This service loads the full hand-written solving guide
    (`angr_summary.md`) and exposes the section for one specific level, keyed
    by `challenge_stage`, so the agent has a complete reference to fall back
    on or defer to when the KG's answer conflicts with it.
    """

    _LEVEL_HEADER_RE = re.compile(r"^## Level (\d{2}) — `([^`]+)`", re.MULTILINE)

    def __init__(self, file_path: str):
        self.__logger = logging.getLogger(__name__)
        self.__sections: dict[int, str] = {}

        path = Path(file_path)
        if not path.is_file():
            self.__logger.warning(f"CTF reference file not found: {path}")
            return

        self.__sections = self.__parse(path.read_text(encoding="utf-8"))
        self.__logger.info(f"Loaded CTF reference guide: {len(self.__sections)} level sections")

    def __parse(self, text: str) -> dict[int, str]:
        matches = list(self._LEVEL_HEADER_RE.finditer(text))
        sections: dict[int, str] = {}
        for i, match in enumerate(matches):
            level = int(match.group(1))
            start = match.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            sections[level] = text[start:end].strip()
        return sections

    def get_reference(self, challenge_stage: int | None) -> str | None:
        """Ground-truth guide section for a challenge stage, if one exists.

        `challenge_stage` follows `app.challenges.UNLOCK_SEQUENCE`: stage 0 is
        pre_study, stage N (1-18) is level N-1 (`ctf_00`..`ctf_17`).
        """
        if challenge_stage is None:
            return None
        return self.__sections.get(challenge_stage - 1)
