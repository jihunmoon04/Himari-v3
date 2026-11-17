// Keeps a timeline at its bottom while new content comes, unless the reader scrolled up; then ``atBottom`` is false
// and ``jump`` brings them back down (the "Jump to the latest" button).
import { useCallback, useLayoutEffect, useRef, useState } from "react";

const NEAR = 80;

export function useStickToBottom(dependency: unknown) {
  const box = useRef<HTMLDivElement>(null);
  const stuck = useRef(true);
  const [atBottom, setAtBottom] = useState(true);
  useLayoutEffect(() => {
    const node = box.current;
    if (node && stuck.current) node.scrollTop = node.scrollHeight;
  }, [dependency]);
  const onScroll = useCallback(() => {
    const node = box.current;
    if (!node) return;
    stuck.current = node.scrollHeight - node.scrollTop - node.clientHeight < NEAR;
    setAtBottom(stuck.current);
  }, []);
  const jump = useCallback(() => {
    const node = box.current;
    if (!node) return;
    stuck.current = true;
    setAtBottom(true);
    node.scrollTo({ top: node.scrollHeight, behavior: "smooth" });
  }, []);
  return { box, onScroll, atBottom, jump };
}
