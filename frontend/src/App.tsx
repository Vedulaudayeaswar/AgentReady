import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import type { ReactNode } from "react";
import { Loader2 } from "lucide-react";
import { useAuth } from "@/hooks/useAuth";
import { lazy } from "react";
import AppLayout from "@/layouts/AppLayout";
import Landing from "@/pages/Landing";
import { ForgotPassword, Login, ResetPassword, Signup } from "@/pages/auth/AuthPages";

// Route-level code splitting: each area of the app (and recharts) is downloaded when first opened,
// so signing in does not download every page of the product.
const named = <T extends Record<string, unknown>, K extends keyof T>(load: () => Promise<T>, key: K) =>
  lazy(() => load().then((m) => ({ default: m[key] as React.ComponentType })));
const Overview = lazy(() => import("@/pages/app/Overview"));
const ConnectWebsite = lazy(() => import("@/pages/app/ConnectWebsite"));
const Catalog = lazy(() => import("@/pages/app/Catalog"));
const ProductDetailPage = lazy(() => import("@/pages/app/ProductDetail"));
const ValidationCenter = lazy(() => import("@/pages/app/ValidationCenter"));
const ExportCenter = lazy(() => import("@/pages/app/ExportCenter"));
const ScanHistory = lazy(() => import("@/pages/app/ScanHistory"));
const ScanDetail = lazy(() => import("@/pages/app/ScanDetail"));
const SettingsPage = lazy(() => import("@/pages/app/Settings"));
const ProductCreate = lazy(() => import("@/pages/app/ProductCreate"));
const Categories = lazy(() => import("@/pages/app/Categories"));
const SystemStatusPage = lazy(() => import("@/pages/app/SystemStatus"));
const commercePages = () => import("@/pages/app/CommercePages");
const InventoryPage = named(commercePages, "InventoryPage");
const PricingPage = named(commercePages, "PricingPage");
const VariantsPage = named(commercePages, "VariantsPage");
const opsPages = () => import("@/pages/app/OpsPages");
const ApiFeedsPage = named(opsPages, "ApiFeedsPage");
const AuditPage = named(opsPages, "AuditPage");
const IntegrationsPage = named(opsPages, "IntegrationsPage");
const SyncPage = named(opsPages, "SyncPage");
const distributionPages = () => import("@/pages/app/DistributionPages");
const AgentApiPage = named(distributionPages, "AgentApiPage");
const DistributionAnalyticsPage = named(distributionPages, "DistributionAnalyticsPage");
const DistributionOverviewPage = named(distributionPages, "DistributionOverviewPage");
const PublicProductsPage = named(distributionPages, "PublicProductsPage");
const distributionOps = () => import("@/pages/app/DistributionOps");
const FeedsPage = named(distributionOps, "FeedsPage");
const PlatformsPage = named(distributionOps, "PlatformsPage");
const WebhooksPage = named(distributionOps, "WebhooksPage");
const intelligencePages = () => import("@/pages/app/IntelligencePages");
const AnomaliesPage = named(intelligencePages, "AnomaliesPage");
const DiscoveryPage = named(intelligencePages, "DiscoveryPage");
const IntelligenceOverviewPage = named(intelligencePages, "IntelligenceOverviewPage");
const QualityPage = named(intelligencePages, "QualityPage");
const RecommendationsPage = named(intelligencePages, "RecommendationsPage");
const intelligenceOps = () => import("@/pages/app/IntelligenceOps");
const AssistantPage = named(intelligenceOps, "AssistantPage");
const InventoryInsightsPage = named(intelligenceOps, "InventoryInsightsPage");
const NotificationsPage = named(intelligenceOps, "NotificationsPage");
const PricingInsightsPage = named(intelligenceOps, "PricingInsightsPage");
const ReportsPage = named(intelligenceOps, "ReportsPage");

function FullScreenLoader() {
  return (
    <div className="grid min-h-screen place-items-center text-muted-foreground">
      <Loader2 className="h-5 w-5 animate-spin" />
    </div>
  );
}

function RequireAuth({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth();
  const location = useLocation();
  if (loading) return <FullScreenLoader />;
  if (!user) return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  return <>{children}</>;
}

function GuestOnly({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth();
  if (loading) return <FullScreenLoader />;
  if (user) return <Navigate to="/app" replace />;
  return <>{children}</>;
}

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Landing />} />
      <Route path="/login" element={<GuestOnly><Login /></GuestOnly>} />
      <Route path="/signup" element={<GuestOnly><Signup /></GuestOnly>} />
      <Route path="/forgot-password" element={<ForgotPassword />} />
      <Route path="/reset-password" element={<ResetPassword />} />
      <Route path="/app" element={<RequireAuth><AppLayout /></RequireAuth>}>
        <Route index element={<Overview />} />
        <Route path="connect" element={<ConnectWebsite />} />
        <Route path="catalog" element={<Catalog />} />
        <Route path="catalog/new" element={<ProductCreate />} />
        <Route path="catalog/:productId" element={<ProductDetailPage />} />
        <Route path="categories" element={<Categories />} />
        <Route path="inventory" element={<InventoryPage />} />
        <Route path="pricing" element={<PricingPage />} />
        <Route path="variants" element={<VariantsPage />} />
        <Route path="sync" element={<SyncPage />} />
        <Route path="integrations" element={<IntegrationsPage />} />
        <Route path="api" element={<ApiFeedsPage />} />
        <Route path="audit" element={<AuditPage />} />
        <Route path="intelligence" element={<IntelligenceOverviewPage />} />
        <Route path="intelligence/quality" element={<QualityPage />} />
        <Route path="intelligence/discovery" element={<DiscoveryPage />} />
        <Route path="intelligence/recommendations" element={<RecommendationsPage />} />
        <Route path="intelligence/anomalies" element={<AnomaliesPage />} />
        <Route path="intelligence/pricing" element={<PricingInsightsPage />} />
        <Route path="intelligence/inventory" element={<InventoryInsightsPage />} />
        <Route path="intelligence/reports" element={<ReportsPage />} />
        <Route path="intelligence/notifications" element={<NotificationsPage />} />
        <Route path="intelligence/assistant" element={<AssistantPage />} />
        <Route path="distribution" element={<DistributionOverviewPage />} />
        <Route path="distribution/products" element={<PublicProductsPage />} />
        <Route path="distribution/agent-api" element={<AgentApiPage />} />
        <Route path="distribution/feeds" element={<FeedsPage />} />
        <Route path="distribution/platforms" element={<PlatformsPage />} />
        <Route path="distribution/webhooks" element={<WebhooksPage />} />
        <Route path="distribution/analytics" element={<DistributionAnalyticsPage />} />
        <Route path="validation" element={<ValidationCenter />} />
        <Route path="exports" element={<ExportCenter />} />
        <Route path="scans" element={<ScanHistory />} />
        <Route path="scans/:scanId" element={<ScanDetail />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="system" element={<SystemStatusPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
