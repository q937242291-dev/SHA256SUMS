"""Load the prompt templates used by localization, guidance, and reranking."""
from pathlib import Path


DIRECTORY = Path(__file__).with_name("prompt_templates")


def load_prompt(name):
    if not name.replace("_", "").isalnum():
        raise ValueError("Invalid prompt name")
    return (DIRECTORY / (name + ".txt")).read_text(encoding="utf-8").strip()


def catalog():
    return {p.stem: p.read_text(encoding="utf-8").strip() for p in sorted(DIRECTORY.glob("*.txt"))}
