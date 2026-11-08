"""The languages a translation model can be listed for: their codes (ISO 639-1, or 639-3 where
a language has no two-letter code), and their names in English, which go into a translation
model's prompt ("Translate the following text from Ukrainian into German"). The page names
them itself, in the person's own language."""

from __future__ import annotations

LANGUAGE_NAMES: dict[str, str] = {
    "an": "Aragonese",
    "ar": "Arabic",
    "ast": "Asturian",
    "bg": "Bulgarian",
    "ca": "Catalan",
    "cs": "Czech",
    "cy": "Welsh",
    "da": "Danish",
    "de": "German",
    "el": "Greek",
    "en": "English",
    "es": "Spanish",
    "et": "Estonian",
    "eu": "Basque",
    "fi": "Finnish",
    "fr": "French",
    "ga": "Irish",
    "gl": "Galician",
    "he": "Hebrew",
    "hi": "Hindi",
    "hr": "Croatian",
    "hu": "Hungarian",
    "id": "Indonesian",
    "it": "Italian",
    "ja": "Japanese",
    "ko": "Korean",
    "lt": "Lithuanian",
    "lv": "Latvian",
    "mt": "Maltese",
    "nb": "Norwegian Bokmål",
    "nl": "Dutch",
    "nn": "Norwegian Nynorsk",
    "no": "Norwegian",
    "oc": "Occitan",
    "pl": "Polish",
    "pt": "Portuguese",
    "ro": "Romanian",
    "ru": "Russian",
    "sk": "Slovak",
    "sl": "Slovenian",
    "sr": "Serbian",
    "sv": "Swedish",
    "th": "Thai",
    "tr": "Turkish",
    "uk": "Ukrainian",
    "vi": "Vietnamese",
    "zh": "Chinese",
}


def name(code: str) -> str:
    """A language's English name; the code itself for one this list doesn't have."""
    return LANGUAGE_NAMES.get(code, code)
