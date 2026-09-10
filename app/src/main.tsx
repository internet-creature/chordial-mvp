import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./styles.css";
import { watchTheme } from "./lib/theme";

// the theme lands before the first paint; both windows watch the same choice
watchTheme(window);

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
