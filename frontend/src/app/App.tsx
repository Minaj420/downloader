import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, NavLink, Route, Routes } from "react-router-dom";
import { BackendStatus } from "../components/BackendStatus";
import { LibraryPage } from "../pages/LibraryPage";
import { NewDownloadPage } from "../pages/NewDownloadPage";
import { QueuePage } from "../pages/QueuePage";
import { SettingsPage } from "../pages/SettingsPage";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false } },
});

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <div className="app">
          <header className="app-header">
            <div className="brand">
              <span className="brand-mark" aria-hidden="true" />
              <span className="brand-name">Downloader</span>
            </div>
            <nav className="app-nav" aria-label="Primary">
              <NavLink to="/" end>
                New Download
              </NavLink>
              <NavLink to="/queue">Queue</NavLink>
              <NavLink to="/library">Library</NavLink>
              <NavLink to="/settings">Settings</NavLink>
            </nav>
            <BackendStatus />
          </header>
          <main className="app-main">
            <Routes>
              <Route path="/" element={<NewDownloadPage />} />
              <Route path="/queue" element={<QueuePage />} />
              <Route path="/library" element={<LibraryPage />} />
              <Route path="/settings" element={<SettingsPage />} />
            </Routes>
          </main>
        </div>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
