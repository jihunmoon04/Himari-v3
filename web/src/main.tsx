import "./styles.css";
import { QueryClientProvider } from "@tanstack/react-query";
import { setNonce } from "get-nonce";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, Navigate, RouterProvider } from "react-router";
import { queryClient, useSession } from "@/app/queries";
import { Shell } from "@/components/Shell";
import { applyAppearance, watchAppearance } from "@/lib/appearance";
import { Agents } from "@/routes/Agents";
import { Approvals } from "@/routes/Approvals";
import { Conversation } from "@/routes/Conversation";
import { Home } from "@/routes/Home";
import { HubSession } from "@/routes/HubSession";
import { Login } from "@/routes/Login";

// V3-P102 §7.2: the server gave this page a style nonce (its CSP allows <style> elements carrying it). Radix's
// scroll lock inserts such an element; get-nonce hands it the nonce. Before anything renders.
const nonce = document.querySelector<HTMLMetaElement>('meta[name="csp-nonce"]')?.content;
if (nonce && !nonce.startsWith("__")) setNonce(nonce);

applyAppearance();
watchAppearance();

function Root() {
  const session = useSession();
  if (session.isLoading) return null;
  if (!session.data?.authenticated) return <Login />;
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
