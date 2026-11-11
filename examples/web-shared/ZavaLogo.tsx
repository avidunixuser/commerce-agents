import icon from "./assets/zava-icon.png";
import logo from "./assets/zava-logo.png";

export function ZavaLogo({ compact = false }: { compact?: boolean }) {
  const image = compact ? icon : logo;
  return (
    <img
      src={image.src}
      width={image.width}
      height={image.height}
      alt="Zava Agentic AI"
      className={`block h-auto shrink-0 rounded-sm ${compact ? "w-[34px]" : "w-[132px]"}`}
    />
  );
}
