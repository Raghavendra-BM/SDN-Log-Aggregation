import { cn } from "@/lib/utils";
import type { Level } from "@/lib/types";

const STYLES: Record<Level, string> = {
  DEBUG: "bg-level-debug-bg text-level-debug",
  INFO: "bg-level-info-bg text-level-info",
  WARNING: "bg-level-warn-bg text-level-warn",
  ERROR: "bg-level-error-bg text-level-error",
  CRITICAL: "bg-level-critical-bg text-level-critical",
};

// Small flat badge for a log level.
export function LevelBadge({ level }: { level: Level }) {
  return (
    <span className={cn("inline-block w-[68px] rounded-[3px] py-px text-center font-mono text-[11px] font-medium", STYLES[level])}>
      {level}
    </span>
  );
}
