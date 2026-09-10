import { useEffect, useState } from "react";
import { currentTheme, setTheme, watchTheme, type Theme } from "./theme";
import { setNativeTheme } from "./tauriWindow";

/** the theme in force for this window, and the way to choose another.
 * the native window chrome follows whenever the theme changes. */
export function useTheme(): [Theme, (theme: Theme) => void] {
  const [theme, setState] = useState<Theme>(() => currentTheme(window));
  useEffect(
    () =>
      watchTheme(window, (next) => {
        setState(next);
        void setNativeTheme(next);
      }),
    [],
  );
  return [theme, (next) => setTheme(window, next)];
}
