import { useEffect, useState } from "react";
import { currentTheme, setTheme, watchTheme, type Theme } from "./theme";

/** the theme in force for this window, and the way to choose another. */
export function useTheme(): [Theme, (theme: Theme) => void] {
  const [theme, setState] = useState<Theme>(() => currentTheme(window));
  useEffect(() => watchTheme(window, setState), []);
  return [theme, (next) => setTheme(window, next)];
}
