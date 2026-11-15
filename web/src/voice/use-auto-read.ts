import { useState } from "react";

export const AUTO_READ_KEY = "lectern.voice.autoread";

function stored(): boolean {
  try {
    return window.localStorage.getItem(AUTO_READ_KEY) === "1";
  } catch {
    return false; // storage refused (a private window, blocked site data): off
  }
}

/** Whether new replies are read aloud as their turn ends: this browser's choice, kept in its
 * storage, so it is the person's on this device and nobody else's. Off until switched on. */
export function useAutoRead(): [boolean, (on: boolean) => void] {
  const [on, setOn] = useState(stored);
  function change(next: boolean) {
    setOn(next);
    try {
      if (next) window.localStorage.setItem(AUTO_READ_KEY, "1");
      else window.localStorage.removeItem(AUTO_READ_KEY);
    } catch {
      // not kept: it holds for this page until it is reloaded
    }
  }
  return [on, change];
}
