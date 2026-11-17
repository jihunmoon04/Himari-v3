import "./styles.css";
import { QueryClientProvider } from "@tanstack/react-query";
import { setNonce } from "get-nonce";
import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, Navigate, RouterProvider } from "react-router";
import { queryClient, useSession } from "@/app/queries";
import { Shell } from "@/components/Shell";
import { resetFollowed } from "@/features/hub/follow";
import { resetPointed } from "@/features/references";
import { moveLegacyDrafts } from "@/lib/drafts";
import { applyAppearance, watchAppearance } from "@/lib/appearance";
import { Agents } from "@/routes/Agents";
import { Approvals } from "@/routes/Approvals";
import { Conversation } from "@/routes/Conversation";
import { Home } from "@/routes/Home";
import { HubSession } from "@/routes/HubSession";
import { Login } from "@/routes/Login";
import { Settings } from "@/routes/Settings";
import { Trash } from "@/routes/Trash";

// V3-P102 §7.2: the server gave this page a style nonce (its CSP allows <style> elements carrying it). Radix's
// scroll lock inserts such an element; get-nonce hands it the nonce. Before anything renders.
const nonce = document.querySelector<HTMLMetaElement>('meta[name="csp-nonce"]')?.content;
if (nonce && !nonce.startsWith("__")) setNonce(nonce);

applyAppearance();
watchAppearance();

function Root() {
  const session = useSession();
  const known = session.data !== undefined; // asked and answered: before that nothing is decided
  const signedIn = session.data?.authenticated === true;
  const [drafted, setDrafted] = useState(false); // the classic page's drafts moved in (or none to move)
  useEffect(() => {
    if (!known) return;
    if (signedIn) {
      void moveLegacyDrafts().finally(() => setDrafted(true));
      return;
    }
    // signed out or the sign-in ended: what it was following goes (the drafts stay for the same person signing
    // back in; the user's own Sign out wipes them, lib/drafts.ts)
    resetFollowed();
    resetPointed();
    setDrafted(false);
  }, [known, signedIn]);
  if (session.isLoading) return null;
  if (!session.data?.authenticated) return <Login />;
  if (!drafted) return null; // a moment: the composers read their drafts as they open
  return <Shell />;
}

const router = createBrowserRouter(
  [
    {
      path: "/",
      element: <Root />,
      children: [
        { index: true, element: <Home /> },
        { path: "c/:id", element: <Conversation /> },
        { path: "s/:id", element: <HubSession /> },
        { path: "agents", element: <Agents /> },
        { path: "approvals", element: <Approvals /> },
        { path: "trash", element: <Trash /> },
        { path: "settings", element: <Settings /> },
        { path: "settings/:section", element: <Settings /> },
        { path: "*", element: <Navigate to="/" replace /> },
      ],
    },
  ],
  { basename: import.meta.env.BASE_URL.replace(/\/$/, "") },
);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
