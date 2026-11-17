# Anova Precision Oven

A Home Assistant custom integration for the Anova Precision Oven 2.0.

## Install

Needs Home Assistant 2026.10 or newer and [HACS](https://hacs.xyz). It is installed as a custom
repository:

1. In HACS, open the three-dot menu and choose **Custom repositories**.
2. Add `https://github.com/jellyfiche/jelly-anova` with the type **Integration**.
3. Find **Anova Precision Oven** in HACS, download it, and restart Home Assistant.
4. Go to **Settings > Devices & services > Add integration**, choose **Anova Precision Oven**, and sign in
   with your Anova account. Your password is used once and is not saved.

## Camera

The oven's **Camera** shows live video while the oven is cooking, and is unavailable otherwise.
The video comes from Anova's own video service, as in the Anova app. It stops a minute after
the last viewer closes it. There are no still pictures.

## Cook status and Continue

**Cook status** says where a cook is: preheating, cooking, timer finished, or waiting for you
(to start the timer, to put the food in or take it out, or to start the next stage).
**Continue** does what the oven is waiting for, like the Start buttons in the Anova app.

To end a running timer a set time from now, use the **Update cook** action's **Minutes left**.
Its **Timer** field sets the stage's whole length, counted from when the timer started.

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
- [awgneo/anova-culinary](https://github.com/awgneo/anova-culinary) and
  [ampdot-io/anova-oven-mcp](https://github.com/ampdot-io/anova-oven-mcp), for how the live video works;
  awgneo/anova-culinary also for the idea of a cook status and a Continue button, and for how the
  oven counts its timer
