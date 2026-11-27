import { useCallback, useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import * as api from "@/api";
import { isAuthError } from "@/api";
import type { CapInfo } from "@/api";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyTitle,
} from "@/components/ui/empty";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { AuthPage } from "@/components/AuthPage";
import { CapabilityList } from "@/components/CapabilityList";
import { ModelsPage } from "@/components/ModelsPage";
import { SharesPage } from "@/components/SharesPage";

const POLL_MS = 3000;

export default function Dashboard({ onAuthLost }: { onAuthLost: () => void }) {
  const [caps, setCaps] = useState<CapInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [tab, setTab] = useState("capabilities");

  const refresh = useCallback(async () => {
    try {
      setCaps(await api.caps());
      setError(null);
    } catch (err) {
      if (isAuthError(err)) {
        onAuthLost();
        return;
      }
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [onAuthLost]);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), POLL_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  const act = useCallback(
    async (name: string, action: "start" | "stop" | "restart") => {
      setBusy(`${name}:${action}`);
      try {
        await api.control(name, action);
        await refresh();
      } catch (err) {
        if (isAuthError(err)) {
          onAuthLost();
          return;
        }
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setBusy(null);
      }
    },
    [refresh, onAuthLost]
  );

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-4 p-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">capforge console</h1>
          <p className="text-sm text-muted-foreground">
            Capabilities, tags, environment, models, identity providers and shared folders.
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={() => void refresh()}>
          <RefreshCw data-icon="inline-start" />
          Refresh
        </Button>
      </header>

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="capabilities">Capabilities</TabsTrigger>
          <TabsTrigger value="models">Models</TabsTrigger>
          <TabsTrigger value="auth">Authentication</TabsTrigger>
          <TabsTrigger value="shares">Shared folders</TabsTrigger>
        </TabsList>

        <TabsContent value="capabilities" className="flex flex-col gap-4">
          {error && (
            <Alert variant="destructive">
              <AlertTitle>Console request failed</AlertTitle>
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}

          {caps === null ? (
            <div className="flex flex-col gap-2">
              <Skeleton className="h-16 w-full" />
              <Skeleton className="h-16 w-full" />
              <Skeleton className="h-16 w-full" />
            </div>
          ) : caps.length === 0 ? (
            <Empty>
              <EmptyHeader>
                <EmptyTitle>No capabilities yet</EmptyTitle>
                <EmptyDescription>
                  Scaffold one through the MCP surface. It will appear here with its tags,
                  environment and pipeline state.
                </EmptyDescription>
              </EmptyHeader>
            </Empty>
          ) : (
            <CapabilityList
              caps={caps}
              busy={busy}
              onAct={act}
              onChanged={refresh}
              onAuthLost={onAuthLost}
            />
          )}
        </TabsContent>

        <TabsContent value="models">
          <ModelsPage onAuthLost={onAuthLost} />
        </TabsContent>

        <TabsContent value="auth">
          <AuthPage onAuthLost={onAuthLost} />
        </TabsContent>

        <TabsContent value="shares">
          <SharesPage onAuthLost={onAuthLost} />
        </TabsContent>
      </Tabs>
    </div>
  );
}
