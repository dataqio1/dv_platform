import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { PrefsProvider } from "./prefs";
import { ProjectsProvider } from "./activeProject";
import "./index.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <PrefsProvider>
        <ProjectsProvider>
          <App />
        </ProjectsProvider>
      </PrefsProvider>
    </BrowserRouter>
  </React.StrictMode>
);
