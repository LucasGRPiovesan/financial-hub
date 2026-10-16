import { useEffect } from "react";

const BRAND = "Financial Hub";

/** "Chaves de API · Financial Hub": the browser tab and history entries name the current screen, in the current language. */
export function useDocumentTitle(section: string | null): void {
  useEffect(() => {
    document.title = section ? `${section} · ${BRAND}` : BRAND;
  }, [section]);
}
