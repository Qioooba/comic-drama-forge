// 风格缩略图映射：style_id -> import 后的图片 URL。
//
// ⚠️ 为什么图仍在前端 import（不搬到后端）：
//   Flask 只挂了 /assets 路由，61 张 jpg 放在 frontend/src/assets/styles/ 由 Vite
//   打包进产物。搬到后端等于给后端加一条静态路由（且要改 vite base / 缓存策略），
//   收益为零。后端 style_catalog.json 只给「文件名」，URL 仍由这张表解析。
//
// ⚠️ 为什么不按文件名反查：style_id 是**语义标识**，与图片文件名**故意解耦**
//   （例如 style_3d_cg_urban 用的是 style_3d_luxury_fantasy.jpg 那张图）。
//   所以两边靠 style_id 显式对齐，改名/换图都不会漏。
//
// 本文件零 CJK 约定：字符串一律 \u 转义（注释用中文，与仓库其它前端文件一致）。
import imgStyle2dUrbanRomance from '@/assets/styles/style_2d_urban_romance.jpg';
import imgStyle2dGuofengDongman from '@/assets/styles/style_2d_guofeng_dongman.jpg';
import imgStyleBw2dComic from '@/assets/styles/style_bw_2d_comic.jpg';
import imgStyle2dKoreanWebtoon from '@/assets/styles/style_2d_korean_webtoon.jpg';
import imgStyle2dRetroAmerican from '@/assets/styles/style_2d_retro_american.jpg';
import imgStyle2dInkGuofeng from '@/assets/styles/style_2d_ink_guofeng.jpg';
import imgStyle2dCelAnime from '@/assets/styles/style_2d_cel_anime.jpg';
import imgStyle2dCyberpunkIllust from '@/assets/styles/style_2d_cyberpunk_illust.jpg';
import imgStyle2d90sAnime from '@/assets/styles/style_2d_90s_anime.jpg';
import imgStyle2dOtomo from '@/assets/styles/style_2d_otomo.jpg';
import imgStyle2dPixel from '@/assets/styles/style_2d_pixel.jpg';
import imgStyle2dTezuka from '@/assets/styles/style_2d_tezuka.jpg';
import imgStyle2dAnime from '@/assets/styles/style_2d_anime.jpg';
import imgStyle2dMiyazaki from '@/assets/styles/style_2d_miyazaki.jpg';
import imgStyle2dAmericanComicAnim from '@/assets/styles/style_2d_american_comic_anim.jpg';
import imgStyle2dShanghaiStudio from '@/assets/styles/style_2d_shanghai_studio.jpg';
import imgStyle2dStickFigure from '@/assets/styles/style_2d_stick_figure.jpg';
import imgStyle2dShojo from '@/assets/styles/style_2d_shojo.jpg';
import imgStyle2dLooseSketch from '@/assets/styles/style_2d_loose_sketch.jpg';
import imgStyle2dGuochao from '@/assets/styles/style_2d_guochao.jpg';
import imgStyle2dBwInk from '@/assets/styles/style_2d_bw_ink.jpg';
import imgStyle2dChineseMythology from '@/assets/styles/style_2d_chinese_mythology.jpg';
import imgStyle2dCrayon from '@/assets/styles/style_2d_crayon.jpg';
import imgStyle2dShadowPlay from '@/assets/styles/style_2d_shadow_play.jpg';
import imgStyle3dXianxiaCg from '@/assets/styles/style_3d_xianxia_cg.jpg';
import imgStyle3dGuofengCg from '@/assets/styles/style_3d_guofeng_cg.jpg';
import imgStyle3dDarkFantasy from '@/assets/styles/style_3d_dark_fantasy.jpg';
import imgStyle3dAaaConcept from '@/assets/styles/style_3d_aaa_concept.jpg';
import imgStyle3dUe5Urban from '@/assets/styles/style_3d_ue5_urban.jpg';
import imgStyle3dLuxuryFantasy from '@/assets/styles/style_3d_luxury_fantasy.jpg';
import imgStyle3dDisney from '@/assets/styles/style_3d_disney.jpg';
import imgStyle3dFruitPerson from '@/assets/styles/style_3d_fruit_person.jpg';
import imgStyle3dGameRender from '@/assets/styles/style_3d_game_render.jpg';
import imgStyle3dClaymation from '@/assets/styles/style_3d_claymation.jpg';
import imgStyle3dClayStopmotion from '@/assets/styles/style_3d_clay_stopmotion.jpg';
import imgRpPostApocalyptic from '@/assets/styles/rp_post_apocalyptic.jpg';
import imgRpXianxiaReal from '@/assets/styles/rp_xianxia_real.jpg';
import imgRp80sRural from '@/assets/styles/rp_80s_rural.jpg';
import imgRpAncientCostume from '@/assets/styles/rp_ancient_costume.jpg';
import imgRpHongkongFilm from '@/assets/styles/rp_hongkong_film.jpg';
import imgRpKoreanDrama from '@/assets/styles/rp_korean_drama.jpg';
import imgRpUrbanReal from '@/assets/styles/rp_urban_real.jpg';
import imgRpAmericanUpturn from '@/assets/styles/rp_american_upturn.jpg';
import imgRpAmericanVintageTv from '@/assets/styles/rp_american_vintage_tv.jpg';
import imgRpBwPhotography from '@/assets/styles/rp_bw_photography.jpg';
import imgRp90sRealist from '@/assets/styles/rp_90s_realist.jpg';
import imgRpRetroHollywood from '@/assets/styles/rp_retro_hollywood.jpg';
import imgRpBlueOrange from '@/assets/styles/rp_blue_orange.jpg';
import imgRpHighKeyAbsurd from '@/assets/styles/rp_high_key_absurd.jpg';
import imgRpSuspense from '@/assets/styles/rp_suspense.jpg';
import imgRpQuentin from '@/assets/styles/rp_quentin.jpg';
import imgRpRetroFuturism from '@/assets/styles/rp_retro_futurism.jpg';
import imgRpRussianMelancholy from '@/assets/styles/rp_russian_melancholy.jpg';
import imgRpKoreeda from '@/assets/styles/rp_koreeda.jpg';
import imgRpKoreanCold from '@/assets/styles/rp_korean_cold.jpg';
import imgRpWarFilm from '@/assets/styles/rp_war_film.jpg';
import imgRpHorror from '@/assets/styles/rp_horror.jpg';
import imgRpCourtIntrigue from '@/assets/styles/rp_court_intrigue.jpg';
import imgRpWilderness from '@/assets/styles/rp_wilderness.jpg';
import imgRp60sScifi from '@/assets/styles/rp_60s_scifi.jpg';
import imgRpAncientChineseReal from '@/assets/styles/rp_ancient_chinese_real.jpg';

/** style_id -> 缩略图 URL（61 条，与后端 style_catalog.json 一一对应）。 */
export const STYLE_THUMBNAILS: Record<string, string> = {
  // 2D 24
  style_2d_urban_romance: imgStyle2dUrbanRomance,
  style_2d_guofeng_dongman: imgStyle2dGuofengDongman,
  style_bw_2d_comic: imgStyleBw2dComic,
  style_2d_korean_webtoon: imgStyle2dKoreanWebtoon,
  style_2d_retro_american: imgStyle2dRetroAmerican,
  style_2d_ink_guofeng: imgStyle2dInkGuofeng,
  style_2d_cel_anime: imgStyle2dCelAnime,
  style_2d_cyberpunk_illust: imgStyle2dCyberpunkIllust,
  style_2d_90s_anime: imgStyle2d90sAnime,
  style_2d_otomo: imgStyle2dOtomo,
  style_2d_pixel: imgStyle2dPixel,
  style_2d_tezuka: imgStyle2dTezuka,
  style_2d_anime: imgStyle2dAnime,
  style_2d_miyazaki: imgStyle2dMiyazaki,
  style_2d_american_comic_anim: imgStyle2dAmericanComicAnim,
  style_2d_shanghai_studio: imgStyle2dShanghaiStudio,
  style_2d_stick_figure: imgStyle2dStickFigure,
  style_2d_shojo: imgStyle2dShojo,
  style_2d_loose_sketch: imgStyle2dLooseSketch,
  style_2d_guochao: imgStyle2dGuochao,
  style_2d_bw_ink: imgStyle2dBwInk,
  style_2d_chinese_mythology: imgStyle2dChineseMythology,
  style_2d_crayon: imgStyle2dCrayon,
  style_2d_shadow_play: imgStyle2dShadowPlay,
  // 3D 11
  style_3d_xianxia_cg: imgStyle3dXianxiaCg,
  style_3d_guofeng_cg: imgStyle3dGuofengCg,
  style_3d_dark_fantasy: imgStyle3dDarkFantasy,
  style_3d_aaa_concept: imgStyle3dAaaConcept,
  style_3d_ue5_urban: imgStyle3dUe5Urban,
  // style_id 与图片文件名刻意解耦：这张图渲染的是「3D写实CG现代都市风」
  style_3d_cg_urban: imgStyle3dLuxuryFantasy,
  style_3d_disney: imgStyle3dDisney,
  style_3d_fruit_person: imgStyle3dFruitPerson,
  style_3d_game_render: imgStyle3dGameRender,
  style_3d_claymation: imgStyle3dClaymation,
  style_3d_clay_stopmotion: imgStyle3dClayStopmotion,
  // 真人写实 26
  rp_post_apocalyptic: imgRpPostApocalyptic,
  rp_xianxia_real: imgRpXianxiaReal,
  // 同上：文件名 80s_rural 对应的条目语义是「年代剧真人写实风」
  rp_period_drama: imgRp80sRural,
  rp_ancient_costume: imgRpAncientCostume,
  rp_hongkong_film: imgRpHongkongFilm,
  rp_korean_drama: imgRpKoreanDrama,
  rp_urban_real: imgRpUrbanReal,
  rp_american_upturn: imgRpAmericanUpturn,
  rp_american_vintage_tv: imgRpAmericanVintageTv,
  rp_bw_photography: imgRpBwPhotography,
  rp_90s_realist: imgRp90sRealist,
  rp_retro_hollywood: imgRpRetroHollywood,
  rp_blue_orange: imgRpBlueOrange,
  rp_high_key_absurd: imgRpHighKeyAbsurd,
  rp_suspense: imgRpSuspense,
  rp_quentin: imgRpQuentin,
  rp_retro_futurism: imgRpRetroFuturism,
  rp_russian_melancholy: imgRpRussianMelancholy,
  rp_koreeda: imgRpKoreeda,
  rp_korean_cold: imgRpKoreanCold,
  rp_war_film: imgRpWarFilm,
  rp_horror: imgRpHorror,
  rp_court_intrigue: imgRpCourtIntrigue,
  rp_wilderness: imgRpWilderness,
  rp_60s_scifi: imgRp60sScifi,
  rp_ancient_chinese_real: imgRpAncientChineseReal,
};

/** 取缩略图 URL；未登记时返回空串（调用方走纯文字卡片，不崩）。 */
export function thumbFor(styleId: string): string {
  return STYLE_THUMBNAILS[styleId] || '';
}

/** 表内条目数（自检用：应与后端 61 一致）。 */
export const THUMBNAIL_COUNT = Object.keys(STYLE_THUMBNAILS).length;