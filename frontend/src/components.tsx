import { useEffect, useRef, type ReactNode } from "react";
import { AlertCircle, CheckCircle2, Loader2, X } from "lucide-react";
import { statusName } from "./types";
export function IconButton({
  label,
  children,
  onClick,
  disabled,
  className = "",
}: {
  label: string;
  children: ReactNode;
  onClick: () => void;
  disabled?: boolean;
  className?: string;
}) {
  return (
    <button
      type="button"
      className={`icon-button ${className}`}
      aria-label={label}
      title={label}
      onClick={onClick}
      disabled={disabled}
    >
      {children}
    </button>
  );
}
export function Status({ value }: { value?: string }) {
  const s = value?.toUpperCase() ?? "";
  return (
    <span
      className={`status ${["REVIEWED", "SUCCEEDED", "COMPLETE", "AVAILABLE", "CONFIRMED", "VERIFIED", "SUCCEEDED_VERIFIED"].includes(s) ? "good" : ["UNKNOWN", "INCOMPLETE", "REVIEW_REQUIRED", "PAUSED"].includes(s) ? "warn" : ["FAILED", "QUARANTINED", "ERROR"].includes(s) ? "bad" : ""}`}
    >
      <span />
      {statusName(value)}
    </span>
  );
}
export function Empty({
  icon,
  title,
  description,
  action,
}: {
  icon: ReactNode;
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      {icon}
      <strong>{title}</strong>
      {description && <p>{description}</p>}
      {action}
    </div>
  );
}
export function Loading({ text = "正在读取…" }: { text?: string }) {
  return (
    <div className="loading" role="status">
      <Loader2 className="spin" size={18} />
      {text}
    </div>
  );
}
export function Notice({
  children,
  kind = "warn",
}: {
  children: ReactNode;
  kind?: "warn" | "good" | "bad" | "info";
}) {
  return (
    <div
      className={`notice ${kind}`}
      role={kind === "bad" ? "alert" : undefined}
    >
      {kind === "good" ? <CheckCircle2 size={17} /> : <AlertCircle size={17} />}
      <div>{children}</div>
    </div>
  );
}
export function Modal({
  title,
  children,
  onClose,
  wide = false,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  wide?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const prior = document.activeElement as HTMLElement;
    const node = ref.current;
    node?.focus();
    const handler = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeRef.current();
      if (event.key === "Tab") {
        const elements = node?.querySelectorAll<HTMLElement>(
          'button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex="0"]',
        );
        if (!elements?.length) return;
        const first = elements[0],
          last = elements[elements.length - 1];
        if (event.shiftKey && (document.activeElement === first || document.activeElement === node)) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", handler);
    return () => {
      document.removeEventListener("keydown", handler);
      prior?.focus();
    };
  }, []);
  return (
    <div
      className="modal-overlay"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        className={`modal ${wide ? "wide" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        ref={ref}
      >
        <div className="modal-heading">
          <h2>{title}</h2>
          <IconButton label="关闭" onClick={onClose}>
            <X size={19} />
          </IconButton>
        </div>
        {children}
      </div>
    </div>
  );
}
export function dateText(value?: string | number) {
  if (!value) return "未记录";
  const date = new Date(typeof value === 'number' ? value * 1000 : value);
  return Number.isNaN(date.getTime())
    ? String(value)
    : date.toLocaleString("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      });
}
export function StructuredData({ value }: { value: unknown }) {
  if (value == null) return <span className="muted">未获取</span>;
  if (typeof value !== "object") return <span>{String(value)}</span>;
  if (Array.isArray(value))
    return (
      <div className="data-list">
        {value.map((item, index) => (
          <div key={index}>
            <StructuredData value={item} />
          </div>
        ))}
      </div>
    );
  return (
    <dl className="data-grid">
      {Object.entries(value).map(([key, item]) => (
        <div key={key}>
          <dt>{key}</dt>
          <dd>
            <StructuredData value={item} />
          </dd>
        </div>
      ))}
    </dl>
  );
}
