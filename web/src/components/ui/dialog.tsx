import * as Primitive from "@radix-ui/react-dialog";
import type { ReactNode } from "react";

export function Dialog({ open, onOpenChange, title, description, children }: {
  open: boolean; onOpenChange: (open: boolean) => void; title: string; description?: string; children: ReactNode;
}) {
  return (
    <Primitive.Root open={open} onOpenChange={onOpenChange}>
      <Primitive.Portal>
        <Primitive.Overlay className="fixed inset-0 z-40 bg-black/40" />
        <Primitive.Content className="fixed left-1/2 top-1/2 z-50 w-[min(28rem,calc(100vw-2rem))] -translate-x-1/2
          -translate-y-1/2 rounded-2xl border border-line bg-panel p-5 text-ink shadow-xl focus:outline-none">
          <Primitive.Title className="text-base font-semibold">{title}</Primitive.Title>
          {description && <Primitive.Description className="mt-1 text-sm text-muted">{description}</Primitive.Description>}
          <div className="mt-4">{children}</div>
        </Primitive.Content>
      </Primitive.Portal>
    </Primitive.Root>
  );
}

export const DialogClose = Primitive.Close;
