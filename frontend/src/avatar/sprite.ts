/** sprite.ts — Seymour himself, cell for cell.
 *
 * This sprite was EXTRACTED from Nick's reference bitmap (the standing-
 * monster intensity CSV, 780×1305 grayscale): the source's native pixel
 * pitch (6.62px) was recovered from its outline edges, every native cell
 * was classified by region (flood fill + connected components), and the
 * result is this 86×105 grid — so the avatar now IS the reference,
 * not an approximation of it. Regenerate with scripts/extract_sprite.py
 * if the reference art ever changes.
 *
 * One character per cell, mapped onto the live palette at draw time
 * (scene.ts), which is what keeps the sprite tintable by theme, hue and
 * the Game Boy screen option:
 *   '.' transparent   'k' line (ink)   'b' body (skin)   's' bodyDim
 *   'w' eye white     'o' horn         'd' horn ridge    't' tongue
 */

/** The grid: 86 columns × 105 rows (row 0 = horn tips, last = toes). */
export const SPRITE_W = 86;
export const SPRITE_H = 105;
export const SEYMOUR: string[] = [
  "......................................................................................",
  ".......................kkk............................................................",
  ".....................kkook...................................kkk......................",
  "....................ksoodk...................................kookk....................",
  "...................kooosk....................................ksoodk...................",
  "..................koooodk.................kk..................kooodk..................",
  ".................ksooosk..................kkk.................kooook..................",
  ".................koooosk..................kksk.................kooosk.................",
  "................koosssdk.............kkk..kkbkk................koooodk................",
  "................koooosdk..............skkkkkbsk..kkk...........kssossk................",
  "................dooooosk..............kkskksbsksksk............doososd................",
  "...............koooooookk.............kkskkbbskkssk...........koooossdk...............",
  "...............koooooosdk.............kkbsbbsskbskskk.........kooooosdk...............",
  "...............koooooodsdk.......kkkkkkksbbssssskkk..........kdsoooosdk...............",
  "...............ksooodsoodkk...kkkssbbbbbsbsssbskssskkkk.....kdoosooosdk...............",
  "...............ksosdoooosdkkkksbbbbbbbbbbbbbbbbbbbbbssskks.kkoooosoosdk...............",
  "................ddsoooosdkkksbbbbbbbbbbbbbbbbbbbbbbbbbbsskkkosoooossdk................",
  "................ksoooosdkdbbbbbbbbbbbbbbbbbbbbbbbbbbbbbsssksoosooosddk................",
  "................kdsssskkdobbbbbbbbbbbbbbbbbbbbbbbbbbbbbbsbsksossossdsk................",
  ".................kdsdkkooobbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbsdksssssddk.................",
  ".................kkskkooobbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbsdkdsdddkk.................",
  "..................kkkoooobbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbsskksskk..................",
  "..................skbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbssskkkk...................",
  "..................kbbbbbbssssbbbbbbbbssssbbbbbbbbbbbssbbbbbbbssssskk..................",
  ".................ksbbbbbssbsssbbbbbbsssbssbbbbbbbbbsssssbbbbbbsssssk..................",
  ".................kbbbbbsbbbbbbbbbbbbbbbbbbsbbbbbbbssbbbssbbbbbssssssk.................",
  "................ksbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbsbbbbbbsssssk.................",
  "................kbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbsssssk................",
  "................sbbbbbbbbsssbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbsssssk................",
  "...............kbbbbbbbskkkkksbbbbbbskkkksbbbbbbbbbbbbbbbbbbbbbbssssssk...............",
  "...............kbbbbbwskswwwskswbbwskswwwkkswwbwwsskkkkswwwbbbbbssssssk...............",
  "...............sbbbbbskwwwwwwwksbbkkswwwwwskswbwskswwwwskswbbbbbbsssssk...............",
  "..............kbbbbbkkwwwwwwwwwksbkswwwwwwwskwbskwwwwwwwsksbbbbbbssssssk..............",
  "..............kbbbbbkwwwwwwwwwwssskwwwwwwwwwksskwwwwwwwwwskbbbbbbssssssk..............",
  "..............kbbbbsswwwskkswwwwskswwwssswwwsksswwwwwwwwwwksbbbbbbsssssk..............",
  "..............sbbbbkwwwkwkkkswwwkkwwwkkkkwwwwkkwwwwkkkwwwwskbbbbbbsssssk..............",
  ".............ksbbbbkwwwkkkkkkwwwkkwwkkkkkkwwwkkwwwkwkkkwwwwkbbbbbbssssskk.............",
  ".............kbbbbbkwwwkkkkkkwwwkkwwkkkkkkwwwkkwwwkkkkkwwwwkbbbbbbssssssk.............",
  ".............kbbbbbkwwwwkkkkswwwkkwwkkkkkkwwwkkwwwkkkkkwwwwkbbbbbbbsssssk.............",
  ".............kbbbbbkwwwwwkkswwwskkwwwkkkkwwwwkkwwwkkkkwwwwwkbbbbbbbsssssk.............",
  "............ksbbbbbsswwwwwwwwwwkksswwwwwwwwwskkwwwwwwwwwwwssbbbbbbssssssk.............",
  "............kbbbbbbskwwwwwwwwwskskkwwwwwwwwwkskswwwwwwwwwwksbbbbbbsssssskk............",
  "............kbbbbbbbskwwwwwwwwksbskwwwwwwwssksskwwwwwwwwwskbbbbbbbsssssssk............",
  "............kbbbbbbbsskwwwwwskksbbkkwwwwwskkswbkswwwwwwwsksbbbbbbbsssssssk............",
  "...........kbbbbbbbbbsskkkkkksswbbwkkswskkksswbskkswwwskkswbbbbbbbssssssssk...........",
  "...........kbbbbbbbbbbbssssssbbbbbbbskkkkksbbbbbbskkkkkssbbbbbbbbbbswsssssk...........",
  "..........ksbbbbbbbsbbbbbbbbbbbbbbbbbsssssbbbbbbbbsssssbbbbbbbbbbbsswssssssk..........",
  ".........ksbbbbbbbbsbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbsbbbbbbbbssssssssk..........",
  ".........kbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbsswsssssk.........",
  "........ksbbbbbbssbbbsbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbsbbbsbbbbbbbbbsssssskk........",
  ".......kkbbbbbbbbbbbbbbbbbbbkkkbbbbbbbbbbbbsbbbsksbbbbbbsbbbsbbbbbbbbbbbsssssk........",
  ".......kbbbbbbbbbbbsbbbbbbbbskkkkkssbbbbbbbbsskkksbbbbbbbbsbbbbbbbbbbbbbbsssssk.......",
  "......ksbbbbbbbbbbbbbbbbbbbbbkkkkkkkkkkkkkkkkkkkkbbbbbbbbbsbbbbbbbbbbbbbbbsssskk......",
  "......kbbbbbbbbbbbbbbbbbbbbbbkkkkkkkkkkkkkkkkkkkbbbbbbbbbbbbbbbbbbbbbbbbbbsssssk......",
  ".....kbbbbbsbbbbbbbbbbbbbbbbbbkkkkkkkkkkkkkkkkkkbbbbbbbbbbbbbbbbbbbbbbbbbbbsbsssk.....",
  ".....kbbbbbsbbbbbbbbbbbbbbbbbbskkkksttttttttskkbbbbbbbbbbbbbbbbbbbbbbbbbbbbsssssk.....",
  "....kbbbbbssbbbbbbbbbbbbbbbbbbbkkttsttttttttskbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbsssskk....",
  "...ksbbbbbkbbbbbbbbbbbbbbbbbbbbbbkkstttttkkksbbbbbbbbbbbbbbbbbbbssbbbbbbbbbbbssssk....",
  "...kbbbbbskbbbbbbbbbbbbbbbbbbbbbbbbkkkkkkkbbbbbbbbbbbbbbbbbbbbbbssbbbbbbbbbbbsssssk...",
  "..kbbbbbbksbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbssbbbbbbbbbbbssssk...",
  "..kbbbbbbkbbbbbbbbbbbbbbbbbbbbbbbsssbbbbsbbbbbbbbbbbbbbbbbbbbbbbsskbbbbbbbbbbbssssk...",
  "..sbbbbbskbbbbbbbbbbbbbbbbbbbbbbbbbssssssbbbbbbbbbbbbbbbbbbbbbbbsskbbbbbbbbbbbsssssk..",
  ".kbbbbbbskbbbbbbbbbbbbbbbbbbbbbbbbbbsssbbbbbbbbbbbbbbbbbbbbbbbbbssksbbbbbbbbbbbssssk..",
  ".kbbbbbbksbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbsskkbbbbbbbbbbbssssk..",
  ".kbbbbbsksbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbsssskbbbbbbbbbbbssssk..",
  "ksbbbbbskbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbsssskbbbbbbbbbbbsssssk.",
  "ksbbbbbskbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbsssskbbbbbbbbbbbsssssk.",
  "kbbbbbsskbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbsssskbbbbbbbbbbbsssssk.",
  "kbbbbbsskbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbssssskbbbbbbbbbbssssssk.",
  "kbbbbbsskbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbsbssskbbbbbbbbbbssssssk.",
  "kbbbbbsskbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbsbsssksbbbbbbbbbbssssssk.",
  "kbbbbbsskbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbsssskbbbbbbbbbbbssssssk.",
  "ksbbbssskbsbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbsssssskbbbbbbbbbbsssssssk.",
  ".sbbbssskssbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbssssksbbbbbbbbbsbsssssk..",
  ".kbbbsssksssbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbsssssksbbsbbbbbbsssssssk..",
  ".ksbsbssskbssbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbsssssssksbbsbbbbbssssssssk..",
  "..kssssskkssbsbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbsbsssssssksskbbbbbbsssskskk..",
  "..kssssssksssbsbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbsbbsssssssksskbbbbssssssksk...",
  "...kksssssksbsbsbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbsssssssssskksbbsssksssksk....",
  "....kkssskkssssbsbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbssssssssssssssksssssskssskk.....",
  "......kkk.kksssssbsbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbsssbsssssssssbssssksssskkskkk......",
  "...........kssssssbsssbbbbbbbbbbbbbbbbbbbbbbbbbbbssbbssssssssssssssssskkkkbkks........",
  "...........kksssssssbssssbbbbbbbbbbbbbbbbbbbbbsssbsssssssssssssssssssssk..............",
  "............kbssssssssbsbbsbsbsbbbbbbbbbsssssbbbssssssssssssssssssssssks..............",
  "............kssssssssssssssssbbssssbsbbbbssbssssssssssssssssssssssssssk...............",
  ".............kbssssssssssbsssssbbbbbsssssssssssssssssssssssssssssssssk................",
  ".............kbbsssssssssssssssssssbbssssssssssssssssssssssssssssssssk................",
  ".............ksbbbsssssssssssssssssssssssssssssssssssssssssssssssssssk................",
  "..............kbbbbsssssssssssssssssssssssssssssssssssssssssssssssssk.................",
  "..............kbbbbbssssssssssssssssssssssssssssssssssssssssssssssssk.................",
  "..............ksbbbbbsbsssssskkkkkssssssssssssskssssssssbsssssssssssk.................",
  "..............skssbbbbbbssssssssskkkkkkkkkkkkkksssssssbbbssssssssssk..................",
  "..............kbbbbbbbbbbssssssssssskk.......ksssssssssssssssssssssk..................",
  "............skbbbbbbbbbbbbssssssssskk.........kssssssbbbbbbbssssssskk.................",
  "............kbbbbsbbbbbbbbbsssssssskk.........ksssssbbbbbbbbbbbssbssk.................",
  "...........ksbbbssbbbbbsbbbbssssssssk.........kssssbbbbbbbbbbbsbbssssk................",
  "...........kbbbssbbbbskbbsbbbsssssssk........kksssbbbbsssbbbbbskbbbsssk...............",
  "...........kbbbkbbbbbkbbbbsbbsssssssk........ksssbbbskbbbkbbbbbssbbbssk...............",
  "...........kssskbbbbskbbbbssssssssskk........kssssbbsbbbbssbbbbbksbbssk...............",
  "...........kkssksbbssksbbbssssssskkk.........kkssssbkbbbbbkbbbbsksbsskk...............",
  ".............kkkkssssksssssssskkkks...........kksssskbbsssksbssskssskk................",
  "................skkkkkkkkkkkkk..................kkssksssskksssskkkkkk.................",
  "..................................................kkkkkkkkkkkkkk......................",
  "......................................................................................",
  "......................................................................................",
];

/** Face anchors, in CELL coordinates — measured from the same extraction,
 *  used by scene.ts to overdraw expressions (blinks, glances, mouths)
 *  without disturbing the rest of the sprite. */
export const HEAD_TOP = 11;            // head-outline crown row (blit anchor)
export const EYE_Y = 37;               // the shared eye-center row
export const EYE_XS = [26, 39, 52];    // left / middle / right eye centers
export const EYE_RX = 6;               // eye outline half-width (they touch)
export const EYE_RY = 8;               // …and half-height (slightly tall)
export const PUPIL_R = 3;              // the resting pupil radius
/** The face bands an overdraw may erase: [x0, y0, x1, y1] inclusive. */
export const EYE_BAND: [number, number, number, number] = [19, 29, 60, 46];
export const MOUTH_BOX: [number, number, number, number] = [27, 50, 51, 59];
