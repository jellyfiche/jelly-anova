# Jelly Anova

A Home Assistant custom integration for the Anova Precision Oven 2.0.

## Install

Needs Home Assistant 2026.10 or newer and [HACS](https://hacs.xyz). It is installed as a custom
repository:

1. In HACS, open the three-dot menu and choose **Custom repositories**.
2. Add `https://github.com/jellyfiche/jelly-anova` with the type **Integration**.
3. Find **Jelly Anova** in HACS, download it, and restart Home Assistant.
4. Go to **Settings > Devices & services > Add integration**, choose **Jelly Anova**, and sign in
   with your Anova account. Your password is used once and is not saved.

## Unofficial, and it can break

This project is unofficial. It is not made, supported or endorsed by Anova.
It talks to Anova's cloud in a way Anova has not published, so Anova can
change or block that at any time and this integration may stop working
without warning. Use it at your own risk.

## Credits

This builds on the work of others who worked out how the oven talks:

- [andr83/hacs-anova-oven](https://github.com/andr83/hacs-anova-oven)
- [XVincentX/hacs-anova-oven](https://github.com/XVincentX/hacs-anova-oven)
- [bogd/anova-oven-api](https://github.com/bogd/anova-oven-api)
