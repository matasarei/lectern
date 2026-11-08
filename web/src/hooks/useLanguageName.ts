import { useTranslation } from "react-i18next";

/** A language's name in the page's language ("Ukrainian", "українська"), from its code. The
 * code itself when the browser has no name for it. */
export function useLanguageName(): (code: string) => string {
  const { i18n } = useTranslation();
  let names: Intl.DisplayNames | null = null;
  try {
    names = new Intl.DisplayNames([i18n.language], { type: "language" });
  } catch {
    names = null;
  }
  return (code) => {
    try {
      return names?.of(code) ?? code;
    } catch {
      return code; // not a language code the browser accepts
    }
  };
}
