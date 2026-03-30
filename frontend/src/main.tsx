import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Routes, Route } from "react-router-dom";
import "./index.css";
import Navbar from "./components/Navbar.tsx";
import App from "./App.tsx";
import QueryPlanPage from "./pages/QueryPlan.tsx";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <Navbar />
      <Routes>
        <Route path="/" element={<App />} />
        <Route path="/plan" element={<QueryPlanPage />} />
      </Routes>
    </BrowserRouter>
  </StrictMode>,
);
