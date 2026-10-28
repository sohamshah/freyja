// The island's backdrop: a masked NSVisualEffectView behind the web view,
// so a transparent Electron window can carry real frosted glass in any shape.
#import <Cocoa/Cocoa.h>
#define NAPI_VERSION 8
#include <node_api.h>

@interface FreyjaGlassView : NSVisualEffectView
@property (nonatomic) CGFloat radiusTop;
@property (nonatomic) CGFloat radiusBottom;
@end
@implementation FreyjaGlassView
@end

static NSView *contentViewFrom(napi_env env, napi_value handle) {
  void *data = NULL;
  size_t len = 0;
  if (napi_get_buffer_info(env, handle, &data, &len) != napi_ok || data == NULL || len < sizeof(void *)) return nil;
  void *ptr = *(void **)data;
  if (ptr == NULL) return nil;
  id obj = (__bridge id)ptr;
  if (![obj isKindOfClass:[NSView class]]) return nil;
  return (NSView *)obj;
}

static FreyjaGlassView *findGlass(NSView *content) {
  for (NSView *v in content.subviews) {
    if ([v isKindOfClass:[FreyjaGlassView class]]) return (FreyjaGlassView *)v;
  }
  return nil;
}

/* A 9-slice mask: square or round top corners, round bottom corners. */
static NSImage *maskFor(CGFloat rt, CGFloat rb) {
  CGFloat R = MAX(rt, rb);
  CGFloat w = 2 * R + 3, h = rt + rb + 3;
  NSImage *img = [NSImage imageWithSize:NSMakeSize(w, h) flipped:NO drawingHandler:^BOOL(NSRect rect) {
    [[NSColor blackColor] setFill];
    // Bottom corners: a rounded rect of radius rb covers the whole box.
    if (rb > 0) [[NSBezierPath bezierPathWithRoundedRect:NSMakeRect(0, 0, w, h) xRadius:rb yRadius:rb] fill];
    else NSRectFill(NSMakeRect(0, 0, w, h - rt));
    // The band between the corner rows is always solid.
    NSRectFill(NSMakeRect(0, rb, w, h - rb - rt));
    // Top corners.
    if (rt > 0) [[NSBezierPath bezierPathWithRoundedRect:NSMakeRect(0, h - 2 * rt - 1, w, 2 * rt + 1) xRadius:rt yRadius:rt] fill];
    else NSRectFill(NSMakeRect(0, rb, w, h - rb));
    return YES;
  }];
  img.capInsets = NSEdgeInsetsMake(rt + 1, R + 1, rb + 1, R + 1);
  img.resizingMode = NSImageResizingModeStretch;
  return img;
}

static FreyjaGlassView *ensureGlass(NSView *content) {
  FreyjaGlassView *g = findGlass(content);
  if (g) return g;
  g = [[FreyjaGlassView alloc] initWithFrame:NSZeroRect];
  g.blendingMode = NSVisualEffectBlendingModeBehindWindow;
  g.state = NSVisualEffectStateActive;
  g.material = NSVisualEffectMaterialHUDWindow;
  g.wantsLayer = YES;
  g.autoresizingMask = NSViewNotSizable;
  g.radiusTop = -1;
  g.radiusBottom = -1;
  [content addSubview:g positioned:NSWindowBelow relativeTo:nil];
  return g;
}

static double num(napi_env env, napi_value v, double fallback) {
  double d = fallback;
  if (napi_get_value_double(env, v, &d) != napi_ok) return fallback;
  return d;
}

static napi_value boolOut(napi_env env, bool ok) {
  napi_value out;
  napi_get_boolean(env, ok, &out);
  return out;
}

// attach(handle) -> bool
static napi_value Attach(napi_env env, napi_callback_info info) {
  size_t argc = 1;
  napi_value argv[1];
  napi_get_cb_info(env, info, &argc, argv, NULL, NULL);
  NSView *content = argc >= 1 ? contentViewFrom(env, argv[0]) : nil;
  if (!content) return boolOut(env, false);
  ensureGlass(content);
  return boolOut(env, true);
}

// detach(handle) -> bool
static napi_value Detach(napi_env env, napi_callback_info info) {
  size_t argc = 1;
  napi_value argv[1];
  napi_get_cb_info(env, info, &argc, argv, NULL, NULL);
  NSView *content = argc >= 1 ? contentViewFrom(env, argv[0]) : nil;
  FreyjaGlassView *g = content ? findGlass(content) : nil;
  if (!g) return boolOut(env, false);
  [g removeFromSuperview];
  return boolOut(env, true);
}

// setShape(handle, x, y, w, h, radiusTop, radiusBottom) in CSS px from the top-left -> bool
static napi_value SetShape(napi_env env, napi_callback_info info) {
  size_t argc = 7;
  napi_value argv[7];
  napi_get_cb_info(env, info, &argc, argv, NULL, NULL);
  NSView *content = argc >= 5 ? contentViewFrom(env, argv[0]) : nil;
  if (!content) return boolOut(env, false);
  FreyjaGlassView *g = ensureGlass(content);
  CGFloat x = num(env, argv[1], 0), y = num(env, argv[2], 0), w = num(env, argv[3], 0), h = num(env, argv[4], 0);
  CGFloat rt = argc >= 6 ? num(env, argv[5], 0) : 0;
  CGFloat rb = argc >= 7 ? num(env, argv[6], 0) : 0;
  rt = MAX(0, MIN(rt, MIN(w, h) / 2));
  rb = MAX(0, MIN(rb, MIN(w, h) / 2));
  CGFloat fy = content.isFlipped ? y : (NSHeight(content.bounds) - y - h);
  if (g.radiusTop != rt || g.radiusBottom != rb) {
    g.maskImage = maskFor(rt, rb);
    g.radiusTop = rt;
    g.radiusBottom = rb;
  }
  g.frame = NSMakeRect(x, fy, w, h);
  g.hidden = (w <= 0 || h <= 0);
  return boolOut(env, true);
}

// setStyle(handle, dark, material) -> bool. material: an NSVisualEffectMaterial raw value, or -1 for the default.
static napi_value SetStyle(napi_env env, napi_callback_info info) {
  size_t argc = 3;
  napi_value argv[3];
  napi_get_cb_info(env, info, &argc, argv, NULL, NULL);
  NSView *content = argc >= 2 ? contentViewFrom(env, argv[0]) : nil;
  if (!content) return boolOut(env, false);
  FreyjaGlassView *g = ensureGlass(content);
  bool dark = true;
  napi_get_value_bool(env, argv[1], &dark);
  double material = argc >= 3 ? num(env, argv[2], -1) : -1;
  g.appearance = [NSAppearance appearanceNamed:dark ? NSAppearanceNameDarkAqua : NSAppearanceNameAqua];
  if (material >= 0) g.material = (NSVisualEffectMaterial)(long)material;
  else g.material = dark ? NSVisualEffectMaterialHUDWindow : NSVisualEffectMaterialPopover;
  return boolOut(env, true);
}

// describe(handle) -> string, for the probes.
static napi_value Describe(napi_env env, napi_callback_info info) {
  size_t argc = 1;
  napi_value argv[1];
  napi_get_cb_info(env, info, &argc, argv, NULL, NULL);
  NSView *content = argc >= 1 ? contentViewFrom(env, argv[0]) : nil;
  NSString *s = content
    ? [NSString stringWithFormat:@"%@ flipped=%d bounds=%@ window=%@ subviews=%lu glass=%@", NSStringFromClass([content class]), content.isFlipped, NSStringFromRect(content.bounds), NSStringFromClass([content.window class]), (unsigned long)content.subviews.count, findGlass(content) ? NSStringFromRect(findGlass(content).frame) : @"none"]
    : @"no view";
  napi_value out;
  napi_create_string_utf8(env, s.UTF8String, NAPI_AUTO_LENGTH, &out);
  return out;
}

NAPI_MODULE_INIT() {
  napi_property_descriptor props[] = {
    {"attach", NULL, Attach, NULL, NULL, NULL, napi_default, NULL},
    {"detach", NULL, Detach, NULL, NULL, NULL, napi_default, NULL},
    {"setShape", NULL, SetShape, NULL, NULL, NULL, napi_default, NULL},
    {"setStyle", NULL, SetStyle, NULL, NULL, NULL, napi_default, NULL},
    {"describe", NULL, Describe, NULL, NULL, NULL, napi_default, NULL},
  };
  napi_define_properties(env, exports, sizeof(props) / sizeof(props[0]), props);
  return exports;
}
