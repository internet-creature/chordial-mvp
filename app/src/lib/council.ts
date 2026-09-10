// each member's hue for avatar rings and name chips: muted, legible on both themes.
export const COUNCIL_HUES: Record<string, string> = {
  vel: "#e8b3c3",
  pip: "#dab58c",
  skip: "#accfa4",
  remy: "#bcb2d9",
  mabel: "#dca8b7",
  juniper: "#97c9bd",
  edwin: "#a1bbd0",
};
export const memberHue = (id: string): string => COUNCIL_HUES[id] ?? "#accfa4";
