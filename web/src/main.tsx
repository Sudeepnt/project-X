import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Toaster } from "sonner";
import { TooltipProvider } from "@/components/ui";
import { ConfirmHost } from "@/components/ui/confirm";
import { ApiError } from "@/lib/api";
import { router } from "./router";
import "./styles.css";

// Apply the saved theme before first paint to avoid a flash.
const saved = localStorage.getItem("px-theme") ?? "system";
const dark = saved === "dark" || (saved === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
document.documentElement.dataset.theme = dark ? "dark" : "light";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      refetchOnWindowFocus: true,
      retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
    },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <RouterProvider router={router} context={{ queryClient }} />
        <ConfirmHost />
        <Toaster position="bottom-right" toastOptions={{ className: "!rounded-[14px] !text-[14px]" }} />
      </TooltipProvider>
    </QueryClientProvider>
  </StrictMode>,
);
