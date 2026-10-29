// The island's backdrop: a pane of system glass behind the web view, so a
// transparent Electron window can carry real glass in exactly the island's
// shape. On macOS 26 it is NSGlassEffectView (Liquid Glass: the content
// behind the window bends at the rim and lights the pane); earlier systems
// get a masked NSVisualEffectView blur.
#import <Cocoa/Cocoa.h>
#import <objc/runtime.h>
#define NAPI_VERSION 8
#include <node_api.h>

#if !__has_include(<AppKit/NSGlassEffectView.h>)
// An older SDK: the class is looked up at runtime, this only names its API.
typedef NS_ENUM(NSInteger, NSGlassEffectViewStyle) { NSGlassEffectViewStyleRegular, NSGlassEffectViewStyleClear };
@interface NSGlassEffectView : NSView
@property CGFloat cornerRadius;
@property (nullable, copy) NSColor *tintColor;
@property NSGlassEffectViewStyle style;
@end
#endif

/* The legacy blur, for macOS before 26. */
@interface FreyjaBlurView : NSVisualEffectView
@property (nonatomic) NSString *maskKey;
@end
@implementation FreyjaBlurView
@end

static const void *kGlassKey = &kGlassKey;

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

static NSView *findGlass(NSView *content) {
  NSView *g = objc_getAssociatedObject(content, kGlassKey);
  return (g && g.superview == content) ? g : nil;
}

static BOOL liquidAvailable(void) {
  return NSClassFromString(@"NSGlassEffectView") != nil;
}

/* The exact mask for the legacy blur: square or round top corners, round
   bottom corners, drawn at the view's size (a nine-slice image did not
   survive AppKit's slicing: it left the sheet's sides un-blurred). */
static NSImage *maskFor(CGFloat w, CGFloat h, CGFloat rt, CGFloat rb) {
  NSImage *img = [NSImage imageWithSize:NSMakeSize(w, h) flipped:NO drawingHandler:^BOOL(NSRect rect) {
    [[NSColor blackColor] setFill];
    NSBezierPath *p = [NSBezierPath bezierPath];
    // Clockwise from the top-left, y up: the top edge is at h.
    [p moveToPoint:NSMakePoint(rt, h)];
    [p lineToPoint:NSMakePoint(w - rt, h)];
    if (rt > 0) [p appendBezierPathWithArcWithCenter:NSMakePoint(w - rt, h - rt) radius:rt startAngle:90 endAngle:0 clockwise:YES];
    [p lineToPoint:NSMakePoint(w, rb)];
    if (rb > 0) [p appendBezierPathWithArcWithCenter:NSMakePoint(w - rb, rb) radius:rb startAngle:0 endAngle:-90 clockwise:YES];
    [p lineToPoint:NSMakePoint(rb, 0)];
    if (rb > 0) [p appendBezierPathWithArcWithCenter:NSMakePoint(rb, rb) radius:rb startAngle:-90 endAngle:-180 clockwise:YES];
    [p lineToPoint:NSMakePoint(0, h - rt)];
    if (rt > 0) [p appendBezierPathWithArcWithCenter:NSMakePoint(rt, h - rt) radius:rt startAngle:180 endAngle:90 clockwise:YES];
    [p closePath];
    [p fill];
    return YES;
  }];
  return img;
}

static NSAppearance *appearanceFor(BOOL dark) {
  return [NSAppearance appearanceNamed:dark ? NSAppearanceNameDarkAqua : NSAppearanceNameAqua];
}

/* Liquid Glass fixes its light or dark material when the view joins the
   window; a later appearance change does not restyle it. So the view is
   made under the appearance it should have, and remade on a theme change. */
static NSView *makeGlass(NSView *content, BOOL dark, BOOL preferBlur) {
  NSView *g = nil;
  if (!preferBlur && liquidAvailable()) {
    if (@available(macOS 26.0, *)) {
      NSGlassEffectView *lg = [[NSClassFromString(@"NSGlassEffectView") alloc] initWithFrame:NSZeroRect];
      lg.style = NSGlassEffectViewStyleRegular;
      g = lg;
    }
  }
  if (!g) {
    FreyjaBlurView *b = [[FreyjaBlurView alloc] initWithFrame:NSZeroRect];
    b.blendingMode = NSVisualEffectBlendingModeBehindWindow;
    b.state = NSVisualEffectStateActive;
    b.material = dark ? NSVisualEffectMaterialHUDWindow : NSVisualEffectMaterialPopover;
    g = b;
  }
  g.appearance = appearanceFor(dark);
  // The island window always matches its page's shell, so the glass cannot
  // resolve a stale appearance from the window as it joins it.
  content.window.appearance = appearanceFor(dark);
  g.wantsLayer = YES;
  g.autoresizingMask = NSViewNotSizable;
  g.hidden = YES;
  [content addSubview:g positioned:NSWindowBelow relativeTo:nil];
  objc_setAssociatedObject(content, kGlassKey, g, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
  return g;
}

static NSView *ensureGlass(NSView *content) {
  NSView *g = findGlass(content);
  return g ? g : makeGlass(content, YES, NO);
}

static BOOL isBlur(NSView *g) {
  return [g isKindOfClass:[FreyjaBlurView class]];
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

static napi_value strOut(napi_env env, NSString *s) {
  napi_value out;
  napi_create_string_utf8(env, s.UTF8String, NAPI_AUTO_LENGTH, &out);
  return out;
}

// attach(handle, dark?, preferBlur?) -> 'liquid' | 'blur' | null
static napi_value Attach(napi_env env, napi_callback_info info) {
  size_t argc = 3;
  napi_value argv[3];
  napi_get_cb_info(env, info, &argc, argv, NULL, NULL);
  NSView *content = argc >= 1 ? contentViewFrom(env, argv[0]) : nil;
  if (!content) {
    napi_value out;
    napi_get_null(env, &out);
    return out;
  }
  bool dark = true, preferBlur = false;
  if (argc >= 2) napi_get_value_bool(env, argv[1], &dark);
  if (argc >= 3) napi_get_value_bool(env, argv[2], &preferBlur);
  NSView *g = findGlass(content);
  if (g) [g removeFromSuperview];
  g = makeGlass(content, dark, preferBlur);
  return strOut(env, isBlur(g) ? @"blur" : @"liquid");
}

// detach(handle) -> bool
static napi_value Detach(napi_env env, napi_callback_info info) {
  size_t argc = 1;
  napi_value argv[1];
  napi_get_cb_info(env, info, &argc, argv, NULL, NULL);
  NSView *content = argc >= 1 ? contentViewFrom(env, argv[0]) : nil;
  NSView *g = content ? findGlass(content) : nil;
  if (!g) return boolOut(env, false);
  [g removeFromSuperview];
  objc_setAssociatedObject(content, kGlassKey, nil, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
  return boolOut(env, true);
}

// setShape(handle, x, y, w, h, radiusTop, radiusBottom) in CSS px from the top-left -> bool
static napi_value SetShape(napi_env env, napi_callback_info info) {
  size_t argc = 7;
  napi_value argv[7];
  napi_get_cb_info(env, info, &argc, argv, NULL, NULL);
  NSView *content = argc >= 5 ? contentViewFrom(env, argv[0]) : nil;
  if (!content) return boolOut(env, false);
  NSView *g = ensureGlass(content);
  CGFloat x = num(env, argv[1], 0), y = num(env, argv[2], 0), w = num(env, argv[3], 0), h = num(env, argv[4], 0);
  CGFloat rt = argc >= 6 ? num(env, argv[5], 0) : 0;
  CGFloat rb = argc >= 7 ? num(env, argv[6], 0) : 0;
  rt = MAX(0, MIN(rt, MIN(w, h) / 2));
  rb = MAX(0, MIN(rb, MIN(w, h) / 2));
  CGFloat fy = content.isFlipped ? y : (NSHeight(content.bounds) - y - h);
  if (isBlur(g)) {
    FreyjaBlurView *b = (FreyjaBlurView *)g;
    NSString *key = [NSString stringWithFormat:@"%.1f,%.1f,%.1f,%.1f", w, h, rt, rb];
    if (![key isEqualToString:b.maskKey]) {
      b.maskImage = maskFor(w, h, rt, rb);
      b.maskKey = key;
    }
    b.frame = NSMakeRect(x, fy, w, h);
  } else if (@available(macOS 26.0, *)) {
    // Liquid Glass rounds all four corners alike. The island's top corners
    // sit flush under the screen edge, so the pane runs on above the window
    // (where nothing is drawn) and only the bottom corners show.
    NSGlassEffectView *lg = (NSGlassEffectView *)g;
    CGFloat extend = MAX(0, rb - rt);
    lg.cornerRadius = rb;
    lg.frame = NSMakeRect(x, fy, w, h + extend);
  }
  g.hidden = (w <= 0 || h <= 0);
  return boolOut(env, true);
}

// setStyle(handle, dark, material, tintR?, tintG?, tintB?, tintA?) -> bool.
// Liquid: `material` is the NSGlassEffectViewStyle (0 regular, 1 clear) and the tint washes the glass.
// Blur: `material` is an NSVisualEffectMaterial raw value, -1 for the default; the tint is ignored.
static napi_value SetStyle(napi_env env, napi_callback_info info) {
  size_t argc = 7;
  napi_value argv[7];
  napi_get_cb_info(env, info, &argc, argv, NULL, NULL);
  NSView *content = argc >= 2 ? contentViewFrom(env, argv[0]) : nil;
  if (!content) return boolOut(env, false);
  NSView *g = ensureGlass(content);
  bool dark = true;
  napi_get_value_bool(env, argv[1], &dark);
  double material = argc >= 3 ? num(env, argv[2], -1) : -1;
  if (!isBlur(g) && ![g.appearance.name isEqualToString:appearanceFor(dark).name]) {
    // Remade under the new appearance, in the same place.
    NSRect frame = g.frame;
    BOOL hidden = g.hidden;
    CGFloat radius = 0;
    if (@available(macOS 26.0, *)) radius = ((NSGlassEffectView *)g).cornerRadius;
    [g removeFromSuperview];
    g = makeGlass(content, dark, NO);
    if (@available(macOS 26.0, *)) ((NSGlassEffectView *)g).cornerRadius = radius;
    g.frame = frame;
    g.hidden = hidden;
  }
  g.appearance = appearanceFor(dark);
  if (isBlur(g)) {
    FreyjaBlurView *b = (FreyjaBlurView *)g;
    if (material >= 0) b.material = (NSVisualEffectMaterial)(long)material;
    else b.material = dark ? NSVisualEffectMaterialHUDWindow : NSVisualEffectMaterialPopover;
  } else if (@available(macOS 26.0, *)) {
    NSGlassEffectView *lg = (NSGlassEffectView *)g;
    lg.style = material == 1 ? NSGlassEffectViewStyleClear : NSGlassEffectViewStyleRegular;
    double a = argc >= 7 ? num(env, argv[6], 0) : 0;
    lg.tintColor = a > 0 ? [NSColor colorWithSRGBRed:num(env, argv[3], 0) / 255.0 green:num(env, argv[4], 0) / 255.0 blue:num(env, argv[5], 0) / 255.0 alpha:a] : nil;
  }
  return boolOut(env, true);
}

// describe(handle) -> string, for the probes.
static napi_value Describe(napi_env env, napi_callback_info info) {
  size_t argc = 1;
  napi_value argv[1];
  napi_get_cb_info(env, info, &argc, argv, NULL, NULL);
  NSView *content = argc >= 1 ? contentViewFrom(env, argv[0]) : nil;
  if (!content) return strOut(env, @"no view");
  NSView *g = findGlass(content);
  NSMutableArray *kinds = [NSMutableArray array];
  for (NSView *v in content.subviews) [kinds addObject:NSStringFromClass([v class])];
  NSString *extra = @"";
  if (g && !isBlur(g)) {
    if (@available(macOS 26.0, *)) {
      NSGlassEffectView *lg = (NSGlassEffectView *)g;
      extra = [NSString stringWithFormat:@" radius=%.1f style=%ld tint=%@", lg.cornerRadius, (long)lg.style, lg.tintColor ? lg.tintColor.description : @"none"];
    }
  }
  return strOut(env, [NSString stringWithFormat:@"%@ flipped=%d bounds=%@ window=%@ subviews=%@ glass=%@%@ appearance: app=%@ window=%@/%@ view=%@/%@",
    NSStringFromClass([content class]), content.isFlipped, NSStringFromRect(content.bounds), NSStringFromClass([content.window class]),
    [kinds componentsJoinedByString:@","], g ? [NSString stringWithFormat:@"%@ %@", NSStringFromClass([g class]), NSStringFromRect(g.frame)] : @"none", extra,
    NSApp.effectiveAppearance.name, content.window.appearance.name ?: @"nil", content.window.effectiveAppearance.name, g ? (g.appearance.name ?: @"nil") : @"-", g ? g.effectiveAppearance.name : @"-"]);
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
