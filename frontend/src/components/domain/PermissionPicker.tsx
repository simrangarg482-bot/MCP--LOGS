import { groupPermissions } from "@/utils/roles";
import type { PermissionInfo } from "@/types/roles";
import { cn } from "@/utils/cn";

interface PermissionPickerProps {
  catalog: PermissionInfo[];
  /** Selected permission codes. */
  value: string[];
  onChange?: (next: string[]) => void;
  /** View-only (built-in roles): checkboxes are shown but cannot be changed. */
  readOnly?: boolean;
  /** Codes the signed-in user holds. A code outside this set cannot be *added*
   * (the backend refuses to let anyone grant what they lack); one already
   * selected can still be removed. Omit to allow everything. */
  grantable?: readonly string[];
}

/** Permissions as grouped checkboxes, each with a plain-language description. */
export function PermissionPicker({ catalog, value, onChange, readOnly = false, grantable }: PermissionPickerProps) {
  const selected = new Set(value);
  const mine = grantable ? new Set(grantable) : null;

  function toggle(code: string) {
    if (readOnly || !onChange) return;
    onChange(selected.has(code) ? value.filter((c) => c !== code) : [...value, code]);
  }

  return (
    <div className="flex flex-col gap-4" data-testid="permission-picker">
      {groupPermissions(catalog).map(([group, permissions]) => (
        <fieldset key={group} className="flex flex-col gap-1.5">
          <legend className="mb-1 text-xs font-semibold uppercase tracking-wide text-ink-subtle">{group}</legend>
          {permissions.map((permission) => {
            const checked = selected.has(permission.code);
            const notGrantable = !readOnly && mine !== null && !mine.has(permission.code) && !checked;
            const disabled = readOnly || notGrantable;
            return (
              <label
                key={permission.code}
                className={cn(
                  "flex items-start gap-2.5 rounded-lg border border-white/10 px-3 py-2",
                  disabled ? "cursor-default opacity-80" : "cursor-pointer hover:bg-white/[0.04]",
                  checked && "border-accent-border bg-accent-subtle/40",
                )}
                title={notGrantable ? "You can only grant permissions you hold yourself" : undefined}
              >
                <input
                  type="checkbox"
                  className="mt-0.5 h-4 w-4 shrink-0"
                  checked={checked}
                  disabled={disabled}
                  onChange={() => toggle(permission.code)}
                  aria-label={permission.label}
                  data-permission={permission.code}
                />
                <span className="min-w-0">
                  <span className="block text-sm font-medium text-ink">{permission.label}</span>
                  <span className="block text-xs text-ink-muted">{permission.description}</span>
                  <span className="mt-0.5 block font-mono text-[11px] text-ink-subtle">{permission.code}</span>
                </span>
              </label>
            );
          })}
        </fieldset>
      ))}
    </div>
  );
}
