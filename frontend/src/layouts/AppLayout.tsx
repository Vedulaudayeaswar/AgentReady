import { Suspense, useState } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  AlertTriangle,
  Loader2,
  Bell,
  Brain,
  FileText,
  Gauge,
  Lightbulb,
  PackageSearch,
  SearchCheck,
  TrendingUp,
  BarChart3,
  Bot,
  Boxes,
  Eye,
  Rss,
  Share2,
  Sparkles,
  Webhook,
  Download,
  FolderTree,
  Globe,
  History,
  KeyRound,
  Layers,
  LayoutDashboard,
  LogOut,
  Menu,
  Package,
  Plug,
  RefreshCw,
  ScrollText,
  Settings,
  ShieldCheck,
  Tag,
  X,
} from "lucide-react";
import { Logo, ThemeToggle } from "@/components/app/common";
import { Button } from "@/components/ui/primitives";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/overlays";
import { useAuth } from "@/hooks/useAuth";
import { api } from "@/services/api";
import { intelligence } from "@/services/intelligence";
import { cn } from "@/utils/cn";

type NavItem = { to: string; label: string; icon: typeof Package; end?: boolean; badge?: "issues" | "conflicts" | "notifications" };
const NAV: { group: string | null; items: NavItem[] }[] = [
  { group: null, items: [{ to: "/app", label: "Overview", icon: LayoutDashboard, end: true }] },
  { group: "Catalog", items: [
    { to: "/app/catalog", label: "Product Catalog", icon: Package },
    { to: "/app/categories", label: "Categories", icon: FolderTree },
    { to: "/app/variants", label: "Variants", icon: Layers },
    { to: "/app/validation", label: "Validation Center", icon: ShieldCheck, badge: "issues" },
  ] },
  { group: "Commerce", items: [
    { to: "/app/inventory", label: "Inventory", icon: Boxes },
    { to: "/app/pricing", label: "Pricing", icon: Tag },
  ] },
  { group: "Sources", items: [
    { to: "/app/connect", label: "Connect Website", icon: Globe },
    { to: "/app/sync", label: "Synchronization", icon: RefreshCw, badge: "conflicts" },
    { to: "/app/integrations", label: "Integrations", icon: Plug },
    { to: "/app/scans", label: "Scan History", icon: History },
  ] },
  { group: "Commerce Intelligence", items: [
    { to: "/app/intelligence", label: "Intelligence Overview", icon: Brain, end: true },
    { to: "/app/intelligence/quality", label: "Catalog Quality", icon: Gauge },
    { to: "/app/intelligence/discovery", label: "Discovery Insights", icon: SearchCheck },
    { to: "/app/intelligence/recommendations", label: "Recommendations", icon: Lightbulb },
    { to: "/app/intelligence/anomalies", label: "Anomaly Detection", icon: AlertTriangle },
    { to: "/app/intelligence/pricing", label: "Pricing Insights", icon: TrendingUp },
    { to: "/app/intelligence/inventory", label: "Inventory Insights", icon: PackageSearch },
    { to: "/app/intelligence/reports", label: "Reports", icon: FileText },
    { to: "/app/intelligence/notifications", label: "Notifications", icon: Bell, badge: "notifications" },
    { to: "/app/intelligence/assistant", label: "AI Catalog Assistant", icon: Bot },
  ] },
  { group: "Distribution", items: [
    { to: "/app/distribution", label: "Distribution Overview", icon: Share2, end: true },
    { to: "/app/distribution/products", label: "Public Products", icon: Eye },
    { to: "/app/distribution/agent-api", label: "Agent API", icon: Bot },
    { to: "/app/distribution/feeds", label: "Product Feeds", icon: Rss },
    { to: "/app/distribution/platforms", label: "AI Platforms", icon: Sparkles },
    { to: "/app/distribution/webhooks", label: "Webhooks", icon: Webhook },
    { to: "/app/distribution/analytics", label: "Distribution Analytics", icon: BarChart3 },
    { to: "/app/api", label: "Catalog API v2", icon: KeyRound },
    { to: "/app/exports", label: "Export Center", icon: Download },
  ] },
  { group: "Account", items: [
    { to: "/app/audit", label: "Audit Logs", icon: ScrollText },
    { to: "/app/system", label: "System Status", icon: Activity },
    { to: "/app/settings", label: "Settings", icon: Settings },
  ] },
];

function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  const { user, signOut } = useAuth();
  const navigate = useNavigate();
  const { data: overview } = useQuery({ queryKey: ["overview"], queryFn: api.overview, staleTime: 30_000 });
  const openIssues = overview ? overview.open_issues.error + overview.open_issues.warning : 0;
  const conflicts = overview?.commerce?.pending.open_conflicts ?? 0;
  const { data: unread } = useQuery({ queryKey: ["intel-unread"], queryFn: intelligence.unread, staleTime: 30_000, refetchInterval: 60_000 });
  const initials = (user?.full_name || user?.email || "?").slice(0, 1).toUpperCase();

  return (
    <div className="flex h-full flex-col">
      <div className="flex h-14 items-center px-4">
        <Logo to="/app" className="text-[15px]" />
      </div>
      <nav className="flex-1 space-y-3 overflow-y-auto px-2 py-2">
        {NAV.map((section) => (
          <div key={section.group ?? "top"} className="space-y-0.5">
            {section.group && <p className="px-2.5 pb-1 text-[11px] font-medium uppercase tracking-wide text-muted-foreground/70">{section.group}</p>}
        {section.items.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            onClick={onNavigate}
            className={({ isActive }) =>
              cn(
                "group flex items-center gap-2.5 rounded-md px-2.5 py-1.5 text-[13px] font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground",
                isActive && "bg-accent text-foreground",
              )
            }
          >
            <item.icon className="h-4 w-4" />
            <span className="flex-1">{item.label}</span>
            {item.badge === "issues" && openIssues > 0 && (
              <span className="tabular rounded-full bg-muted px-1.5 text-[11px] text-muted-foreground">{openIssues}</span>
            )}
            {item.badge === "notifications" && (unread?.unread ?? 0) > 0 && (
              <span className="tabular rounded-full bg-info/15 px-1.5 text-[11px] text-info" data-testid="unread-badge">{unread!.unread}</span>
            )}
            {item.badge === "conflicts" && conflicts > 0 && (
              <span className="tabular rounded-full bg-warning/15 px-1.5 text-[11px] text-warning">{conflicts}</span>
            )}
          </NavLink>
        ))}
          </div>
        ))}
      </nav>
      <div className="border-t p-2">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button className="flex w-full items-center gap-2.5 rounded-md px-2 py-1.5 text-left hover:bg-accent">
              <span className="grid h-7 w-7 place-items-center rounded-full bg-muted text-xs font-semibold">{initials}</span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[13px] font-medium">{user?.business.name}</span>
                <span className="block truncate text-[11.5px] text-muted-foreground">{user?.email}</span>
              </span>
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="w-56">
            <DropdownMenuLabel>{user?.full_name || user?.email}</DropdownMenuLabel>
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={() => navigate("/app/settings")}>
              <Settings /> Settings
            </DropdownMenuItem>
            <DropdownMenuItem
              onSelect={async () => {
                await signOut();
                navigate("/login");
              }}
            >
              <LogOut /> Sign out
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </div>
  );
}

export default function AppLayout() {
  const [open, setOpen] = useState(false);
  return (
    <div className="min-h-screen bg-background">
      <aside className="fixed inset-y-0 left-0 z-30 hidden w-60 border-r bg-muted/20 lg:block">
        <Sidebar />
      </aside>
      {open && (
        <div className="fixed inset-0 z-40 lg:hidden">
          <div className="absolute inset-0 bg-black/40" onClick={() => setOpen(false)} />
          <aside className="absolute inset-y-0 left-0 w-64 border-r bg-background shadow-xl">
            <Button variant="ghost" size="icon" className="absolute right-2 top-3" onClick={() => setOpen(false)} aria-label="Close menu">
              <X />
            </Button>
            <Sidebar onNavigate={() => setOpen(false)} />
          </aside>
        </div>
      )}
      <div className="lg:pl-60">
        <header className="sticky top-0 z-20 flex h-14 items-center gap-2 border-b bg-background/85 px-4 backdrop-blur lg:px-8">
          <Button variant="ghost" size="icon" className="lg:hidden" onClick={() => setOpen(true)} aria-label="Open menu">
            <Menu />
          </Button>
          <div className="flex-1" />
          <ThemeToggle />
        </header>
        <main className="mx-auto max-w-[1200px] px-4 py-8 lg:px-8 animate-fade-in">
          <Suspense fallback={<div role="status" aria-label="Loading page" className="grid min-h-[40vh] place-items-center text-muted-foreground"><Loader2 className="h-5 w-5 animate-spin" /></div>}>
            <Outlet />
          </Suspense>
        </main>
      </div>
    </div>
  );
}
