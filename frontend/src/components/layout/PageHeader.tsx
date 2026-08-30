import type { ReactNode } from "react";
import type { Breadcrumb } from "./Breadcrumbs";
import { Breadcrumbs } from "./Breadcrumbs";

interface PageHeaderProps {
  title: string;
  description?: string;
  breadcrumbs?: Breadcrumb[];
  actions?: ReactNode;
}

export function PageHeader({ title, description, breadcrumbs, actions }: PageHeaderProps) {
  return (
    <div className="mb-1 flex flex-col gap-2">
      {breadcrumbs && <Breadcrumbs items={breadcrumbs} />}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-[-0.025em] text-ink">{title}</h1>
          {description && <p className="mt-1 max-w-3xl text-sm leading-5 text-ink-muted">{description}</p>}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
      </div>
    </div>
  );
}
