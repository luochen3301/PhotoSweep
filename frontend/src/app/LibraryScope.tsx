import { createContext, useContext, useState, type ReactNode } from "react";

const LibraryScopeContext = createContext<{ libraryId: string; setLibraryId: (id: string) => void }>({
  libraryId: "", setLibraryId: () => {},
});

export function LibraryScopeProvider({ children }: { children: ReactNode }) {
  const [libraryId, setLibraryId] = useState("");
  return <LibraryScopeContext.Provider value={{ libraryId, setLibraryId }}>{children}</LibraryScopeContext.Provider>;
}

export function useLibraryScope() {
  return useContext(LibraryScopeContext);
}
