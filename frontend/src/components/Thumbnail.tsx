import { useEffect, useState } from "react";
import { ImageBroken } from "@phosphor-icons/react";

export function Thumbnail({ src, alt = "" }: { src: string; alt?: string }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [src]);
  return failed ? <span className="media-tile__failed" role="img" aria-label="无法预览"><ImageBroken size={23} /><small>无法预览</small></span>
    : <img src={src} alt={alt} loading="lazy" onError={() => setFailed(true)} />;
}
