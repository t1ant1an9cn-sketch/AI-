import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { initToken } from "./api";
import { App } from "./App";
import "./styles.css";

initToken();
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
