import i18n from "i18next";
import { initReactI18next } from "react-i18next";

import en from "./locales/en/common.json";

export const resources = { en: { common: en } } as const;

void i18n.use(initReactI18next).init({
  resources,
  lng: "en",
  fallbackLng: "en",
  defaultNS: "common",
  interpolation: { escapeValue: false }, // React escapes already
});

export default i18n;
