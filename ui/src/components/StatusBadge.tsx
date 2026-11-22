import { Badge } from "@/components/ui/badge";

export function StatusBadge({
  children,
  warn = false,
  ok = false,
}: {
  children: React.ReactNode;
  warn?: boolean;
  ok?: boolean;
}) {
  return (
    <Badge variant={warn ? "destructive" : ok ? "secondary" : "outline"}>
      {children}
    </Badge>
  );
}

export function ShareAccessBadge({ access }: { access: "ro" | "rw" | null }) {
  if (!access) return <Badge variant="outline">none</Badge>;
  return <Badge variant={access === "rw" ? "default" : "secondary"}>{access}</Badge>;
}
