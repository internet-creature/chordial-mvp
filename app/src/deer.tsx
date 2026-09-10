import React from "react";
import ReactDOM from "react-dom/client";
import DeerWindow from "./components/DeerWindow";
import "./styles.css";
import { watchTheme } from "./lib/theme";
import "./deer.css";

// the theme lands before the first paint; both windows watch the same choice
watchTheme(window);

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <DeerWindow />
  </React.StrictMode>,
);
