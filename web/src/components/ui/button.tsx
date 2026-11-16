import type { ButtonHTMLAttributes } from "react";
import { cn } from "@/lib/cn";

type Variant = "primary" | "ghost" | "outline" | "danger";
const variants: Record<Variant, string> = {
  primary: "bg-accent text-accent-ink hover:opacity-90",
  ghost: "text-muted hover:bg-raised hover:text-ink",
  outline: "border border-line text-ink hover:bg-raised",
  danger: "bg-danger text-white hover:opacity-90",
};

export function Button({ variant = "outline", className, type = "button", ...rest }:
  ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return (
    <button
      type={type}
      className={cn("inline-flex items-center justify-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium",
        "transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent",
        "disabled:pointer-events-none disabled:opacity-40", variants[variant], className)}
      {...rest}
    />
  );
}

export function IconButton({ label, className, type = "button", ...rest }:
  ButtonHTMLAttributes<HTMLButtonElement> & { label: string }) {
  return (
    <button
      type={type}
      aria-label={label}
      title={label}
      className={cn("inline-flex size-8 items-center justify-center rounded-lg text-muted transition-colors",
        "hover:bg-raised hover:text-ink focus-visible:outline-2 focus-visible:outline-accent",
        "disabled:pointer-events-none disabled:opacity-40", className)}
      {...rest}
    />
  );
}
