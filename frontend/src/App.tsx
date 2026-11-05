import { Compass } from "lucide-react";
import { Link, Route, Routes } from "react-router";
import { EmptyState } from "./components/EmptyState";
import { AppLayout } from "./components/layout/AppLayout";
import { Home } from "./pages/Home";
import { ProjectDetail } from "./pages/ProjectDetail";
import { Projects } from "./pages/Projects";
import { RunDetail } from "./pages/RunDetail";
import { Runs } from "./pages/Runs";
import { Templates } from "./pages/Templates";
import { Tools } from "./pages/Tools";

function NotFound() {
  return (
    <div className="mx-auto max-w-xl pt-16">
      <EmptyState icon={Compass} title="This page does not exist" action={<Link to="/" className="text-sm font-medium text-violet-700 hover:underline">Go home</Link>} />
    </div>
  );
}

export function AppRoutes() {
  return (
    <Routes>
      <Route element={<AppLayout />}>
        <Route index element={<Home />} />
        <Route path="projects" element={<Projects />} />
        <Route path="projects/:projectId" element={<ProjectDetail />} />
        <Route path="runs" element={<Runs />} />
        <Route path="runs/:runId" element={<RunDetail />} />
        <Route path="templates" element={<Templates />} />
        <Route path="tools" element={<Tools />} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}
