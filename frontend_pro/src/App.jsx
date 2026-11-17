import { useCallback, useEffect, useState } from "react";
import { ApiError, clearAccessToken, refreshAccessToken, storeAccessToken } from "./api/client.js";
import { getAccount } from "./api/account.js";
import AdminDashboard from "./admin/AdminDashboard.jsx";
import AuthPage from "./auth/AuthPage.jsx";
import InvitePage from "./auth/InvitePage.jsx";
import AppFooter from "./components/AppFooter.jsx";
import AppHeader from "./components/AppHeader.jsx";
import LoadingScreen from "./components/LoadingScreen.jsx";
import WorkspacePage from "./workspace/WorkspacePage.jsx";
import WorkflowPage from "./workflow/WorkflowPage.jsx";

const basePath = import.meta.env.BASE_URL.replace(/\/$/, "");

function routeFromLocation() {
  const pathname = basePath && window.location.pathname.startsWith(basePath) ? window.location.pathname.slice(basePath.length) : window.location.pathname;
  const path = pathname.replace(/\/+$/, "") || "/";
  if (path.endsWith("/invite")) {
    const token = new URLSearchParams(window.location.search).get("token") || "";
    return `invite|${token}`;
  }
  if (path.endsWith("/register")) return "register";
  if (path.endsWith("/login")) return "login";
  const workflow = path.match(/^\/projects\/([^/]+)\/(tender|review|compliance|checklist|bids|evaluate|results|report)$/);
  if (workflow) return `workflow|${decodeURIComponent(workflow[1])}|${workflow[2]}`;
  if (path === "/admin") return "admin";
  return "workspace";
}

function routePath(route) {
  if (route.startsWith("invite|")) {
    const [, token] = route.split("|");
    return `${basePath}/invite${token ? `?token=${encodeURIComponent(token)}` : ""}`;
  }
  if (route.startsWith("workflow|")) {
    const [, projectId, page] = route.split("|");
    return `${basePath}/projects/${encodeURIComponent(projectId)}/${page}`;
  }
  return `${basePath}${{ login: "/login", register: "/register", workspace: "/", admin: "/admin" }[route]}`;
}

export default function App() {
  const [route, setRoute] = useState(routeFromLocation);
  const [account, setAccount] = useState(null);
  const [loading, setLoading] = useState(true);
  const [serviceError, setServiceError] = useState("");
  const navigate = useCallback((next, replace = false) => {
    window.history[replace ? "replaceState" : "pushState"]({}, "", routePath(next));
    setRoute(next);
    window.scrollTo({ top: 0, behavior: "instant" });
  }, []);

  const loadSession = useCallback(async () => {
    setLoading(true);
    setServiceError("");

    // Authentication pages are public. Do not probe the protected account
    // endpoint (or rotate a refresh cookie) while the user is signing in or
    // bootstrapping a workspace; a missing refresh cookie is expected there.
    if (["login", "register"].includes(route)) {
      setAccount(null);
      setLoading(false);
      return;
    }

    try {
      const data = await getAccount();
      setAccount(data);
    } catch (error) {
      setAccount(null);
      if (error instanceof ApiError && [401, 403].includes(error.status)) {
        try {
          const refreshed = await refreshAccessToken();
          setAccount(refreshed);
          if (["login", "register"].includes(route)) navigate("workspace", true);
        } catch {
          clearAccessToken();
          if (route !== "register") navigate("login", true);
        }
      } else setServiceError(error.message || "The workspace service is unavailable.");
    } finally { setLoading(false); }
  }, [navigate, route]);

  useEffect(() => { loadSession(); }, []);
  useEffect(() => {
    const pop = () => setRoute(routeFromLocation());
    window.addEventListener("popstate", pop);
    return () => window.removeEventListener("popstate", pop);
  }, []);

  if (loading) return <LoadingScreen label="Loading workspace" />;
  if (!account) {
    if (route.startsWith("invite|")) {
      const [, token] = route.split("|");
      return (
        <InvitePage
          token={token}
          onAuthenticated={(data) => {
            storeAccessToken(data.access_token);
            setAccount(data);
            navigate("workspace", true);
          }}
          onNavigate={navigate}
        />
      );
    }
    return (
      <AuthPage
        mode={route === "register" ? "register" : "login"}
        serviceError={serviceError}
        onAuthenticated={(data) => {
          storeAccessToken(data.access_token);
          setAccount(data);
          navigate("workspace", true);
        }}
        onNavigate={navigate}
      />
    );
  }

  const workflowRoute = route.startsWith("workflow|") ? route.split("|") : null;
  const replaceProject = (updated) => setAccount((current) => ({
    ...current,
    projects: [updated, ...current.projects.filter((item) => item.project_id !== updated.project_id)],
  }));

  return (
    <div className="flex min-h-screen flex-col bg-[var(--color-canvas)]">
      <a className="skip-link" href="#app-content">Skip to main content</a>
      <AppHeader user={account.user} onAdmin={() => navigate("admin")} />
      <div className="flex-1" id="app-content" tabIndex="-1">
        {route === "admin" ? (
          <AdminDashboard account={account} onBack={() => navigate("workspace")} />
        ) : workflowRoute ? (
          <WorkflowPage account={account} projectId={workflowRoute[1] === "new" ? null : workflowRoute[1]} page={workflowRoute[2]} onBack={() => navigate("workspace")} onNavigate={(projectId, page) => navigate(`workflow|${projectId}|${page}`)} onProjectChange={replaceProject} />
        ) : (
          <WorkspacePage account={account} onAccountChange={setAccount} onNewTender={() => navigate("workflow|new|tender")} onOpenProject={(project, page) => navigate(`workflow|${project.project_id}|${page}`)} onSignedOut={() => { clearAccessToken(); setAccount(null); navigate("login", true); }} />
        )}
      </div>
      <AppFooter />
    </div>
  );
}
