import { useQuery } from "@tanstack/react-query";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { AuthGate } from "./app/AuthGate";
import { Shell } from "./app/Shell";
import { Cleanup } from "./features/Cleanup";
import { Dashboard } from "./features/Dashboard";
import { Duplicates } from "./features/Duplicates";
import { OnboardingPage } from "./features/OnboardingPage";
import { SettingsPage } from "./features/SettingsPage";
import { TrashPage } from "./features/TrashPage";
import { api } from "./lib/api";
import type { OnboardingStatus } from "./types";

export function App() {
  return (
    <AuthGate>
      <AuthenticatedApp />
    </AuthGate>
  );
}

function AuthenticatedApp() {
  const location = useLocation();
  const onboarding = useQuery({
    queryKey: ["onboarding"],
    queryFn: () => api<OnboardingStatus>("/setup/onboarding"),
  });

  if (onboarding.isLoading) {
    return <div className="app-loading">正在检查安装状态…</div>;
  }

  if (location.pathname === "/onboarding") {
    return <OnboardingPage />;
  }

  if (onboarding.data && !onboarding.data.data.has_library && !onboarding.data.data.dismissed) {
    return <Navigate to="/onboarding" replace />;
  }

  return (
    <Shell>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/screenshots" element={<Cleanup category="screenshot" />} />
        <Route
          path="/screen-recordings"
          element={<Cleanup category="screen_recording" />}
        />
        <Route path="/app-images" element={<Cleanup category="app_image" />} />
        <Route path="/duplicate-photos" element={<Duplicates mediaType="photo" />} />
        <Route path="/duplicate-videos" element={<Duplicates mediaType="video" />} />
        <Route path="/trash" element={<TrashPage />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Shell>
  );
}
