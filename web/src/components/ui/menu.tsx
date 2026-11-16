import * as Dropdown from "@radix-ui/react-dropdown-menu";
import type { ComponentProps, ReactNode } from "react";
import { cn } from "@/lib/cn";

export const Menu = Dropdown.Root;
export const MenuTrigger = Dropdown.Trigger;
export const MenuGroup = Dropdown.Group;
export const MenuRadioGroup = Dropdown.RadioGroup;

export function MenuContent({ className, children, ...rest }: ComponentProps<typeof Dropdown.Content>) {
  return (
    <Dropdown.Portal>
      <Dropdown.Content
        sideOffset={6}
        className={cn("z-50 min-w-48 rounded-xl border border-line bg-panel p-1 text-sm text-ink shadow-lg", className)}
        {...rest}
      >
        {children}
      </Dropdown.Content>
    </Dropdown.Portal>
  );
}

const itemClass = "flex cursor-default select-none items-center gap-2 rounded-lg px-2.5 py-1.5 outline-none "
  + "data-[highlighted]:bg-raised data-[disabled]:opacity-40";

export function MenuItem({ className, ...rest }: ComponentProps<typeof Dropdown.Item>) {
  return <Dropdown.Item className={cn(itemClass, className)} {...rest} />;
}

export function MenuRadioItem({ className, children, ...rest }: ComponentProps<typeof Dropdown.RadioItem>) {
  return (
    <Dropdown.RadioItem className={cn(itemClass, "pl-7 relative", className)} {...rest}>
      <Dropdown.ItemIndicator className="absolute left-2.5 text-accent">●</Dropdown.ItemIndicator>
      {children}
    </Dropdown.RadioItem>
  );
}

export function MenuLabel({ children }: { children: ReactNode }) {
  return <Dropdown.Label className="px-2.5 pb-1 pt-2 text-xs font-medium text-faint">{children}</Dropdown.Label>;
}

export const MenuSeparator = () => <Dropdown.Separator className="my-1 h-px bg-line" />;
