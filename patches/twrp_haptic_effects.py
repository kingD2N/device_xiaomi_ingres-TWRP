#!/usr/bin/env python3
# Menu "Efek Haptic" untuk TWRP ingres -- porting fitur haptic OFOX ingres ke TWRP 12.1.
#
#  - Getar biasa TIDAK diubah: tetap lewat sysfs /sys/class/leds/vibrator (sudah teruji).
#  - Efek Click / Double click / Tick / Thud / Pop / Heavy click per kategori (Tombol,
#    Keyboard, Aksi) lewat IVibrator::perform() di HAL qcom (IVibrator/default), cadangan
#    HAL Xiaomi (IVibrator/vibratorfeature) kalau HAL qcom tidak punya efek.
#  - Anti-lag seperti OFOX: semua panggilan HAL di thread terpisah dan memakai
#    AServiceManager_checkService (tidak menunggu), jadi sentuhan tidak pernah tertahan.
#  - Getar halus saat daftar di-scroll (kalau efek Tombol aktif), halaman Tes Haptic.
#  - Menu hanya muncul kalau HAL benar-benar melaporkan efek. Kalau tidak, TWRP sama
#    seperti sebelumnya.
#  - HAL qcom: nama motor ingres "awinic_haptic"/"aw8697_haptic" ditambahkan ke
#    vendor/qcom/opensource/vibrator lalu dibuat penanda .ingres_awinic (dibaca BoardConfig.mk).
#
# Usage: python3 twrp_haptic_effects.py <root source TWRP>   (folder yang berisi bootable/recovery)
# Aman dijalankan berulang. Bagian bootable/recovery all-or-nothing: kalau satu pola tidak
# ketemu, tidak ada file yang diubah (build tetap jalan seperti TWRP biasa).

import os
import sys

MARK = "[ingres-hfx]"


def warn(msg):
    print(f"::warning::twrp_haptic_effects: {msg}")


# ---------------------------------------------------------------- minuitwrp/events.cpp
EVENTS_CODE = r'''
/* [ingres-hfx] Efek haptic AIDL untuk TWRP ingres (porting dari OFOX ingres).
 * Getar biasa tetap lewat vibrate() di atas (sysfs). Efek memakai IVibrator::perform()
 * pada HAL qcom (IVibrator/default) atau HAL Xiaomi (IVibrator/vibratorfeature).
 * Panggilan HAL selalu di thread terpisah + checkService (tidak menunggu): anti-lag. */
#ifndef TW_NO_HAPTICS
#include <aidl/android/hardware/vibrator/IVibrator.h>
#include <android/binder_manager.h>
#include <android/binder_ibinder.h>
#include <algorithm>
#include <atomic>
#include <mutex>
#include <thread>
#include <vector>

namespace {
namespace hfx {
using ::aidl::android::hardware::vibrator::CompositeEffect;
using ::aidl::android::hardware::vibrator::CompositePrimitive;
using ::aidl::android::hardware::vibrator::Effect;
using ::aidl::android::hardware::vibrator::EffectStrength;
using ::aidl::android::hardware::vibrator::IVibrator;

struct Hal {
    const char* instance;
    const char* name;
};
const Hal kHals[] = {
    {"android.hardware.vibrator.IVibrator/default", "qcom"},
    {"android.hardware.vibrator.IVibrator/vibratorfeature", "xiaomi"},
};

std::mutex lock;  // melindungi vib/effects/primitives
std::shared_ptr<IVibrator> vib;
std::vector<Effect> effects;
std::vector<CompositePrimitive> primitives;
std::atomic_int state{-1};  // -1 belum dicek / perlu cek ulang, 0 tanpa efek, 1 efek ON
std::atomic_bool can_compose{false};
std::atomic<const char*> hal_name{"-"};
std::atomic_int on_count{0};
std::atomic_bool probing{false};
std::atomic<long long> last_probe_ms{0};

long long now_ms() {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec * 1000LL + t.tv_nsec / 1000000LL;
}

// Motor tidak selalu dimatikan HAL sendiri: off() setelah durasi habis (seperti FIX_OFF OFOX).
void schedule_off(std::shared_ptr<IVibrator> v, int ms) {
    if (ms <= 0 || ms > 5000) ms = 50;
    on_count++;
    std::thread([v, ms] {
        usleep((ms + 10) * 1000);
        if (--on_count == 0) (void)v->off();
    }).detach();
}

CompositeEffect step(CompositePrimitive p, float scale, int delay_ms) {
    CompositeEffect c;
    c.delayMs = delay_ms;
    c.primitive = p;
    c.scale = scale;
    return c;
}

int compose(const std::vector<CompositeEffect>& comp) {
    if (state != 1 || !can_compose) return -1;
    std::shared_ptr<IVibrator> v;
    {
        std::lock_guard<std::mutex> l(lock);
        if (vib == nullptr) return -1;
        for (const auto& c : comp) {
            if (std::find(primitives.begin(), primitives.end(), c.primitive) == primitives.end())
                return -1;
        }
        v = vib;
    }
    std::thread([v, comp] {
        if (!v->compose(comp, nullptr).isOk()) return;
        int total = 0;
        for (const auto& c : comp) {
            int32_t d = 0;
            if (!v->getPrimitiveDuration(c.primitive, &d).isOk()) d = 20;
            total += c.delayMs + d;
        }
        schedule_off(v, total);
    }).detach();
    return 0;
}
}  // namespace hfx
}  // namespace

// Blocking (memanggil HAL): hanya dari thread latar.
int haptic_effects_probe(void) {
    using namespace hfx;
    std::lock_guard<std::mutex> l(lock);
    if (vib != nullptr && state == 1 && AIBinder_isAlive(vib->asBinder().get()))
        return 1;
    std::shared_ptr<IVibrator> found;
    const char* found_name = "-";
    std::vector<Effect> fe;
    std::vector<CompositePrimitive> fp;
    for (const Hal& h : kHals) {
        ndk::SpAIBinder b(AServiceManager_checkService(h.instance));
        if (b.get() == nullptr) continue;
        std::shared_ptr<IVibrator> v = IVibrator::fromBinder(b);
        if (v == nullptr) continue;
        std::vector<Effect> e;
        if (!v->getSupportedEffects(&e).isOk()) e.clear();
        if (e.empty()) {
            if (found_name[0] == '-') found_name = h.name;
            continue;
        }
        int32_t c = 0;
        if (!v->getCapabilities(&c).isOk()) c = 0;
        std::vector<CompositePrimitive> p;
        if ((c & IVibrator::CAP_COMPOSE_EFFECTS) && !v->getSupportedPrimitives(&p).isOk()) p.clear();
        found = v;
        found_name = h.name;
        fe = e;
        fp = p;
        break;
    }
    const int old = state;
    vib = found;
    effects = fe;
    primitives = fp;
    can_compose = !fp.empty();
    hal_name = found_name;
    state = found != nullptr ? 1 : 0;
    if (state != old) {
        std::string es, ps;
        for (auto x : fe) es += std::to_string(static_cast<int>(x)) + " ";
        for (auto x : fp) ps += std::to_string(static_cast<int>(x)) + " ";
        printf("I:[ingres-hfx] HAL %s effects=[ %s] primitives=[ %s] -> %s\n", found_name,
               es.c_str(), ps.c_str(), state == 1 ? "effects ON" : "effects OFF");
    }
    return state == 1 ? 1 : 0;
}

// Tidak memanggil HAL (aman di thread UI). Kalau perlu dicek ulang, cek di thread latar.
int haptic_effects_supported(void) {
    using namespace hfx;
    if (state == -1 && !probing.exchange(true)) {
        const long long now = now_ms();
        if (now - last_probe_ms < 3000) {
            probing = false;
        } else {
            last_probe_ms = now;
            std::thread([] {
                haptic_effects_probe();
                hfx::probing = false;
            }).detach();
        }
    }
    return state == 1 ? 1 : 0;
}

int haptic_compose_supported(void) {
    return (hfx::state == 1 && hfx::can_compose) ? 1 : 0;
}

const char* haptic_hal_name(void) {
    return hfx::hal_name;
}

int vibrate_effect(int effect, int strength) {
    using namespace hfx;
    if (state != 1) return -1;
    std::shared_ptr<IVibrator> v;
    {
        std::lock_guard<std::mutex> l(lock);
        if (vib == nullptr) return -1;
        if (std::find(effects.begin(), effects.end(), static_cast<Effect>(effect)) == effects.end())
            return -1;
        v = vib;
    }
    if (strength < 0) strength = 0;
    if (strength > 2) strength = 2;
    std::thread([v, effect, strength] {
        int32_t dur = 0;
        if (v->perform(static_cast<Effect>(effect), static_cast<EffectStrength>(strength), nullptr, &dur).isOk())
            schedule_off(v, dur);
        else
            hfx::state = -1;  // HAL error: sementara pakai getar durasi, cek ulang nanti
    }).detach();
    return 0;
}

int vibrate_primitive(int primitive) {
    using namespace hfx;
    if (primitive < 1 || primitive > static_cast<int>(CompositePrimitive::LOW_TICK)) return -1;
    return compose({step(static_cast<CompositePrimitive>(primitive), 1.0f, 0)});
}

int vibrate_compose_pattern(const char* name) {
    using namespace hfx;
    const std::string n = name ? name : "";
    std::vector<CompositeEffect> c;
    if (n == "success") {
        c = {step(CompositePrimitive::CLICK, 0.5f, 0), step(CompositePrimitive::CLICK, 1.0f, 100)};
    } else if (n == "warning") {
        c = {step(CompositePrimitive::CLICK, 1.0f, 0), step(CompositePrimitive::CLICK, 0.5f, 100)};
    } else if (n == "error") {
        c = {step(CompositePrimitive::CLICK, 0.6f, 0), step(CompositePrimitive::CLICK, 0.6f, 80),
             step(CompositePrimitive::CLICK, 1.0f, 80), step(CompositePrimitive::CLICK, 0.6f, 80)};
    } else {
        return -1;
    }
    if (compose(c) == 0) return 0;
    // HAL tanpa compose: pakai efek bawaan yang paling mirip
    if (n == "success") return vibrate_effect(static_cast<int>(Effect::DOUBLE_CLICK), 2);
    if (n == "warning") return vibrate_effect(static_cast<int>(Effect::HEAVY_CLICK), 1);
    return vibrate_effect(static_cast<int>(Effect::THUD), 2);
}
#else
int haptic_effects_probe(void) { return 0; }
int haptic_effects_supported(void) { return 0; }
int haptic_compose_supported(void) { return 0; }
const char* haptic_hal_name(void) { return "-"; }
int vibrate_effect(int, int) { return -1; }
int vibrate_primitive(int) { return -1; }
int vibrate_compose_pattern(const char*) { return -1; }
#endif

'''

MINUI_DECL = '''int vibrate(int timeout_ms);

// [ingres-hfx] Efek haptic AIDL (lihat events.cpp). 0 = berhasil, negatif = tidak didukung
// (pemanggil kembali ke vibrate(durasi)). effect: 0=CLICK 1=DOUBLE_CLICK 2=TICK 3=THUD 4=POP
// 5=HEAVY_CLICK; strength: 0=LIGHT 1=MEDIUM 2=STRONG.
int haptic_effects_probe(void);          // blocking, hanya dari thread latar
int haptic_effects_supported(void);      // cache, aman di thread UI
int haptic_compose_supported(void);
const char* haptic_hal_name(void);
int vibrate_effect(int effect, int strength);
int vibrate_primitive(int primitive);    // CompositePrimitive 1=CLICK .. 8=LOW_TICK
int vibrate_compose_pattern(const char* name); // "success", "error", "warning"
'''

# ---------------------------------------------------------------- data.cpp / data.hpp
DATA_DEFAULTS = '''	mConst.SetValue("tw_disable_haptics", "0");
	// [ingres-hfx] Efek haptic per kategori. enable=0 -> slider durasi biasa.
	// efek: 0=Click 1=Double click 2=Tick 3=Thud 4=Pop 5=Heavy click
	// kekuatan: 0=Ringan 1=Sedang 2=Kuat -1=Mati
	mPersist.SetValue("tw_hfx_button_enable", "0");
	mPersist.SetValue("tw_hfx_button_effect", "0");
	mPersist.SetValue("tw_hfx_button_strength", "1");
	mPersist.SetValue("tw_hfx_keyboard_enable", "0");
	mPersist.SetValue("tw_hfx_keyboard_effect", "2");
	mPersist.SetValue("tw_hfx_keyboard_strength", "0");
	mPersist.SetValue("tw_hfx_action_enable", "0");
	mPersist.SetValue("tw_hfx_action_effect", "5");
	mPersist.SetValue("tw_hfx_action_strength", "2");
	mData.SetValue("tw_hfx_quiet", "0");
	mData.SetValue("tw_hfx_supported", haptic_effects_supported() ? "1" : "0");
	mData.SetValue("tw_hfx_compose", haptic_compose_supported() ? "1" : "0");
	mData.SetValue("tw_hfx_hal", std::string(haptic_hal_name()));
	{
		static bool hfx_probe_started = false;
		if (!hfx_probe_started) {
			hfx_probe_started = true;
			// HAL vibrator bisa start belakangan dari init: cek tiap detik, maksimal ~30 detik
			std::thread([] {
				for (int i = 0; i < 30; i++) {
					int ok = haptic_effects_probe();
					DataManager::SetValue("tw_hfx_hal", std::string(haptic_hal_name()));
					if (ok == 1) {
						DataManager::SetValue("tw_hfx_compose", haptic_compose_supported());
						DataManager::SetValue("tw_hfx_supported", 1);
						return;
					}
					sleep(1);
				}
				LOGINFO("[ingres-hfx] HAL tidak melaporkan efek, menu Efek Haptic disembunyikan\\n");
			}).detach();
		}
	}
'''

DATA_HELPERS = '''#ifndef TW_NO_HAPTICS
// [ingres-hfx] waktu (ms) preview efek terakhir: getar tombol yang ikut terpicu oleh
// sentuhan yang sama dilewati supaya efek yang sedang dicoba tidak tertimpa.
static std::atomic<long long> g_hfx_preview_ms{-100000};

static long long hfx_now_ms(void)
{
	struct timespec t;
	clock_gettime(CLOCK_MONOTONIC, &t);
	return t.tv_sec * 1000LL + t.tv_nsec / 1000000LL;
}

static string hfx_category(const string& varName)
{
	if (varName == "tw_keyboard_vibrate" || varName == "keyboard")
		return "keyboard";
	if (varName == "tw_action_vibrate" || varName == "action")
		return "action";
	return "button";
}

// 0 = efek diputar (atau sengaja dimatikan), -1 = pakai getar durasi biasa
static int hfx_play(const string& cat)
{
	if (DataManager::GetIntValue("tw_hfx_" + cat + "_enable") != 1 || !haptic_effects_supported())
		return -1;
	int strength = DataManager::GetIntValue("tw_hfx_" + cat + "_strength");
	if (strength < 0)
		return 0;  // "Mati"
	return vibrate_effect(DataManager::GetIntValue("tw_hfx_" + cat + "_effect"), strength) == 0 ? 0 : -1;
}
#endif

void DataManager::HapticPreview(const string& category)
{
#ifndef TW_NO_HAPTICS
	const string cat = hfx_category(category);
	g_hfx_preview_ms = hfx_now_ms();
	if (hfx_play(cat) == 0)
		return;
	int ms = GetIntValue("tw_" + cat + "_vibrate");
	if (ms > 0)
		vibrate(ms);
#else
	(void)category;
#endif
}

// Getar halus saat daftar di-scroll (dari OFOX ingres). Hanya kalau efek Tombol aktif.
// speedPct = jarak scroll per event dalam persen tinggi satu item; -1 = awal gesture.
void DataManager::ScrollHaptic(int speedPct)
{
#ifndef TW_NO_HAPTICS
	if (GetIntValue("tw_hfx_button_enable") != 1 || !haptic_effects_supported())
		return;
	if (GetIntValue("tw_hfx_button_strength") < 0 || GetIntValue("tw_hfx_quiet") == 1)
		return;
	static long long last = 0;
	const long long now = hfx_now_ms();
	if (now - last < 40)
		return;  // maksimal ~25 tick per detik
	last = now;
	int strength = 0;  // Ringan
	if (speedPct >= 85)
		strength = 2;  // Kuat
	else if (speedPct >= 52)
		strength = 1;  // Sedang
	vibrate_effect(0 /* CLICK */, strength);
#else
	(void)speedPct;
#endif
}

void DataManager::Vibrate(const string& varName)
{
#ifndef TW_NO_HAPTICS
	// [ingres-hfx]
	if (varName == "tw_button_vibrate" && GetIntValue("tw_hfx_quiet") == 1)
		return;  // halaman Tes Haptic: tombolnya sendiri tidak ikut bergetar
	if (hfx_now_ms() - g_hfx_preview_ms < 250)
		return;  // efek yang sedang dicoba jangan tertimpa
	if (hfx_play(hfx_category(varName)) == 0)
		return;
'''

# ---------------------------------------------------------------- gui/action.cpp
ACTION_IMPL = '''int GUIAction::setbrightness(std::string arg)
{
	return TWFunc::Set_Brightness(arg);
}

// [ingres-hfx] menu Efek Haptic
int GUIAction::hfxpreview(std::string arg)
{
	DataManager::HapticPreview(arg);
	return 0;
}

int GUIAction::hfxeffect(std::string arg)
{
	if (vibrate_effect(atoi(arg.c_str()), 2) != 0)
		LOGINFO("[ingres-hfx] efek %s tidak didukung HAL\\n", arg.c_str());
	return 0;
}

int GUIAction::hfxcompose(std::string arg)
{
	if (vibrate_compose_pattern(arg.c_str()) != 0)
		LOGINFO("[ingres-hfx] compose '%s' tidak didukung HAL\\n", arg.c_str());
	return 0;
}

int GUIAction::hfxprimitive(std::string arg)
{
	if (vibrate_primitive(atoi(arg.c_str())) != 0)
		LOGINFO("[ingres-hfx] primitive %s tidak didukung HAL\\n", arg.c_str());
	return 0;
}
'''


# ---------------------------------------------------------------- theme XML
CATS = [("button", "Tombol", "button_vibration", "Button Vibration:", 300),
        ("keyboard", "Keyboard", "kb_vibration", "Keyboard Vibration:", 300),
        ("action", "Aksi", "act_vibration", "Action Vibration:", 500)]
EFFECTS = ["Click", "Double click", "Tick", "Thud", "Pop", "Heavy click"]
STRENGTHS = [("2", "Kuat"), ("1", "Sedang"), ("0", "Ringan"), ("-1", "Mati")]
CAT_X = ["%indent%", "%btn3_col2_x%", "%btn3_col3_x%"]
CAT_IND_X = ["36", "384", "732"]
STR_X = ["36", "297", "558", "819"]


def cat_page(cat, label, vib_key, vib_def, vib_max):
    en = f"tw_hfx_{cat}_enable"
    x = []
    x.append(f'''		<!-- {MARK} Efek Haptic: {label} -->
		<page name="ingres_hfx_{cat}">
			<template name="page"/>

			<text style="text_l">
				<placement x="%col1_x_header%" y="%row3_header_y%"/>
				<text>{{@settings_hdr=Settings}}</text>
			</text>

			<text style="text_m">
				<placement x="%col1_x_header%" y="%row4_header_y%"/>
				<text>{{@ingres_hfx_hdr=Efek Haptic}}</text>
			</text>

			<template name="tabs_settings"/>

			<fill color="%text_color%">
				<placement x="%tab5_col4_x%" y="%row_tab_y%" w="%tab5_width%" h="%tab_indicator_height%"/>
			</fill>
''')
    for i, (c2, l2, _, _, _) in enumerate(CATS):
        x.append(f'''
			<button style="button_third_width">
				<placement x="{CAT_X[i]}" y="%row3_y%"/>
				<text>{{@ingres_hfx_cat_{c2}={l2}}}</text>
				<action function="page">ingres_hfx_{c2}</action>
			</button>
''')
    i = [c[0] for c in CATS].index(cat)
    x.append(f'''
			<fill color="%highlight%">
				<placement x="{CAT_IND_X[i]}" y="506" w="312" h="6"/>
			</fill>

			<checkbox>
				<placement x="%indent%" y="%row5a_y%"/>
				<text>{{@ingres_hfx_enable=Aktifkan efek haptic}}</text>
				<data variable="{en}"/>
			</checkbox>

			<slidervalue>
				<condition var1="{en}" op="!=" var2="1"/>
				<placement x="indent" y="%row7a_y%" w="%content_width%"/>
				<text>{{@{vib_key}={vib_def}}}</text>
				<data variable="tw_{cat}_vibrate" min="0" max="{vib_max}"/>
			</slidervalue>

			<text style="text_m">
				<condition var1="{en}" op="!=" var2="1"/>
				<placement x="%indent%" y="%row14_y%"/>
				<text>{{@ingres_hfx_off_hint=Efek mati: getar memakai durasi di atas}}</text>
			</text>

			<listbox>
				<condition var1="{en}" var2="1"/>
				<placement x="%indent%" y="%row7a_y%" w="%content_width%" h="656"/>
				<text>{{@ingres_hfx_effects=Pilih efek:}}</text>
				<icon selected="radio_true" unselected="radio_false"/>
				<data name="tw_hfx_{cat}_effect"/>
''')
    for n, name in enumerate(EFFECTS):
        x.append(f'				<listitem name="{name}">{n}<action function="hfxpreview">{cat}</action></listitem>\n')
    x.append(f'''			</listbox>

			<text style="text_m_accent">
				<condition var1="{en}" var2="1"/>
				<placement x="%indent%" y="%row18_y%"/>
				<text>{{@ingres_hfx_strength=Kekuatan efek:}}</text>
			</text>
''')
    for j, (val, lab) in enumerate(STRENGTHS):
        x.append(f'''
			<button style="button_quarter_width">
				<condition var1="{en}" var2="1"/>
				<placement x="{STR_X[j]}" y="%row19_y%"/>
				<text>{{@ingres_hfx_str_{val.replace("-", "m")}={lab}}}</text>
				<actions>
					<action function="set">tw_hfx_{cat}_strength={val}</action>
					<action function="hfxpreview">{cat}</action>
				</actions>
			</button>

			<fill color="%highlight%">
				<condition var1="{en}" var2="1"/>
				<condition var1="tw_hfx_{cat}_strength" var2="{val}"/>
				<placement x="{STR_X[j]}" y="1530" w="225" h="6"/>
			</fill>
''')
    x.append(f'''
			<button style="main_button_half_height">
				<placement x="%indent%" y="%row21a_y%"/>
				<text>{{@ingres_hfx_try=Coba getar}}</text>
				<action function="hfxpreview">{cat}</action>
			</button>

			<button style="main_button_half_height">
				<placement x="%center_x%" y="%row21a_y%"/>
				<text>{{@ingres_hfx_back=Kembali}}</text>
				<action function="page">settings_vibration</action>
			</button>

			<action>
				<touch key="home"/>
				<action function="page">main</action>
			</action>

			<action>
				<touch key="back"/>
				<action function="page">settings_vibration</action>
			</action>
		</page>

''')
    return "".join(x)


def test_page():
    def row(y, items, cond=None):
        out = []
        for i, (lab, func, arg) in enumerate(items):
            c = f'\n				<condition var1="{cond}" var2="1"/>' if cond else ""
            out.append(f'''
			<button style="button_third_width">{c}
				<placement x="{CAT_X[i]}" y="{y}"/>
				<text>{lab}</text>
				<action function="{func}">{arg}</action>
			</button>
''')
        return "".join(out)

    return (f'''		<!-- {MARK} Tes Haptic (efek, compose, primitive) -->
		<page name="ingres_hfx_test">
			<template name="page"/>

			<text style="text_l">
				<placement x="%col1_x_header%" y="%row3_header_y%"/>
				<text>{{@ingres_hfx_test_hdr=Tes Haptic}}</text>
			</text>

			<text style="text_m">
				<placement x="%col1_x_header%" y="%row4_header_y%"/>
				<text>{{@ingres_hfx_test_hal=HAL getar: %tw_hfx_hal%}}</text>
			</text>

			<text style="text_m_accent">
				<placement x="%indent%" y="%row2_y%"/>
				<text>{{@ingres_hfx_test_fx=Efek (kuat):}}</text>
			</text>
'''
            + row("%row3a_y%", [("Click", "hfxeffect", 0), ("Double click", "hfxeffect", 1), ("Tick", "hfxeffect", 2)])
            + row("%row8_y%", [("Thud", "hfxeffect", 3), ("Pop", "hfxeffect", 4), ("Heavy click", "hfxeffect", 5)])
            + '''
			<text style="text_m_accent">
				<placement x="%indent%" y="%row12_y%"/>
				<text>{@ingres_hfx_test_compose=Compose:}</text>
			</text>
'''
            + row("%row13a_y%", [("{@ingres_hfx_ok=Sukses}", "hfxcompose", "success"),
                                 ("{@ingres_hfx_err=Error}", "hfxcompose", "error"),
                                 ("{@ingres_hfx_warn=Peringatan}", "hfxcompose", "warning")])
            + '''
			<text style="text_m_accent">
				<placement x="%indent%" y="%row17_y%"/>
				<text>{@ingres_hfx_test_prim=Primitive:}</text>
			</text>

			<text style="text_m">
				<condition var1="tw_hfx_compose" op="!=" var2="1"/>
				<placement x="%indent%" y="%row18a_y%"/>
				<text>{@ingres_hfx_no_prim=HAL ini tidak mendukung primitive}</text>
			</text>
'''
            + row("%row18a_y%", [("Spin", "hfxprimitive", 3), ("Quick rise", "hfxprimitive", 4),
                                 ("Slow rise", "hfxprimitive", 5)], "tw_hfx_compose")
            + row("%row22a_y%", [("Quick fall", "hfxprimitive", 6), ("Light tick", "hfxprimitive", 7),
                                 ("Low tick", "hfxprimitive", 8)], "tw_hfx_compose")
            + '''
			<action>
				<touch key="home"/>
				<actions>
					<action function="set">tw_hfx_quiet=0</action>
					<action function="page">main</action>
				</actions>
			</action>

			<action>
				<touch key="back"/>
				<actions>
					<action function="set">tw_hfx_quiet=0</action>
					<action function="page">settings_vibration</action>
				</actions>
			</action>
		</page>

''')


VIB_ANCHOR = '''				<data variable="tw_action_vibrate" min="0" max="500"/>
			</slidervalue>
'''
VIB_BUTTONS = VIB_ANCHOR + f'''
			<!-- {MARK} -->
			<button style="main_button_half_height">
				<condition var1="tw_disable_haptics" var2="0"/>
				<condition var1="tw_hfx_supported" var2="1"/>
				<placement x="%indent%" y="%row21a_y%"/>
				<text>{{@ingres_hfx_btn=Efek Haptic}}</text>
				<action function="page">ingres_hfx_button</action>
			</button>

			<button style="main_button_half_height">
				<condition var1="tw_disable_haptics" var2="0"/>
				<condition var1="tw_hfx_supported" var2="1"/>
				<placement x="%center_x%" y="%row21a_y%"/>
				<text>{{@ingres_hfx_test_btn=Tes Haptic}}</text>
				<actions>
					<action function="set">tw_hfx_quiet=1</action>
					<action function="page">ingres_hfx_test</action>
				</actions>
			</button>

			<text style="text_m">
				<condition var1="tw_disable_haptics" var2="0"/>
				<condition var1="tw_hfx_supported" op="!=" var2="1"/>
				<placement x="%indent%" y="%row22_y%"/>
				<text>{{@ingres_hfx_na=Efek haptic belum tersedia (HAL: %tw_hfx_hal%)}}</text>
			</text>
'''


# ---------------------------------------------------------------- patch engine
class Plan:
    def __init__(self, root):
        self.root = root
        self.files = {}
        self.ok = True

    def get(self, rel):
        if rel not in self.files:
            path = os.path.join(self.root, rel)
            try:
                with open(path) as f:
                    self.files[rel] = f.read()
            except OSError as e:
                warn(f"tidak bisa baca {rel}: {e}")
                self.ok = False
                self.files[rel] = None
        return self.files[rel]

    def replace(self, rel, old, new, what):
        src = self.get(rel)
        if src is None:
            return
        n = src.count(old)
        if n != 1:
            warn(f"{rel}: pola '{what}' ketemu {n}x (harus 1x)")
            self.ok = False
            return
        self.files[rel] = src.replace(old, new)

    def write(self):
        for rel, src in self.files.items():
            with open(os.path.join(self.root, rel), "w") as f:
                f.write(src)


def patch_recovery(root):
    rec = os.path.join(root, "bootable", "recovery")
    probe = os.path.join(rec, "minuitwrp", "events.cpp")
    try:
        with open(probe) as f:
            if MARK in f.read():
                print("twrp_haptic_effects: bootable/recovery sudah terpasang")
                return True
    except OSError as e:
        warn(f"{probe}: {e}")
        return False

    p = Plan(rec)
    # minuitwrp
    p.replace("minuitwrp/events.cpp", "/* Returns empty tokens */",
              EVENTS_CODE.lstrip("\n") + "/* Returns empty tokens */", "Returns empty tokens")
    p.replace("minuitwrp/include/minuitwrp/minui.h", "int vibrate(int timeout_ms);\n", MINUI_DECL,
              "int vibrate(int timeout_ms);")
    # data
    p.replace("data.cpp", '#include "minuitwrp/minui.h"\n',
              f'#include "minuitwrp/minui.h"\n#include <atomic> // {MARK}\n#include <thread> // {MARK}\n',
              '#include "minuitwrp/minui.h"')
    p.replace("data.cpp", '\tmConst.SetValue("tw_disable_haptics", "0");\n', DATA_DEFAULTS,
              'tw_disable_haptics "0"')
    p.replace("data.cpp", "void DataManager::Vibrate(const string& varName)\n{\n#ifndef TW_NO_HAPTICS\n",
              DATA_HELPERS, "DataManager::Vibrate")
    p.replace("data.hpp", "\tstatic void Vibrate(const string& varName);\n",
              "\tstatic void Vibrate(const string& varName);\n"
              f"\tstatic void HapticPreview(const string& category); // {MARK}\n"
              f"\tstatic void ScrollHaptic(int speedPct); // {MARK}\n",
              "static void Vibrate")
    # actions
    p.replace("gui/action.cpp", "\t\tADD_ACTION(setbrightness);\n",
              "\t\tADD_ACTION(setbrightness);\n"
              f"\t\tADD_ACTION(hfxpreview);   // {MARK}\n"
              "\t\tADD_ACTION(hfxeffect);\n"
              "\t\tADD_ACTION(hfxcompose);\n"
              "\t\tADD_ACTION(hfxprimitive);\n",
              "ADD_ACTION(setbrightness)")
    p.replace("gui/action.cpp", "int GUIAction::setbrightness(std::string arg)\n{\n"
              "\treturn TWFunc::Set_Brightness(arg);\n}\n", ACTION_IMPL, "GUIAction::setbrightness")
    p.replace("gui/objects.hpp", "\tint setbrightness(std::string arg);\n",
              "\tint setbrightness(std::string arg);\n"
              f"\tint hfxpreview(std::string arg);   // {MARK}\n"
              "\tint hfxeffect(std::string arg);\n"
              "\tint hfxcompose(std::string arg);\n"
              "\tint hfxprimitive(std::string arg);\n",
              "int setbrightness(std::string arg);")
    # scroll tick
    p.replace("gui/objects.hpp", "\tint lastY, last2Y; // last 2 touch locations, used for tracking kinetic scroll speed\n",
              "\tint lastY, last2Y; // last 2 touch locations, used for tracking kinetic scroll speed\n"
              f"\tbool hfxScrollStarted; // {MARK} tick awal gesture scroll sudah diberikan\n",
              "int lastY, last2Y;")
    p.replace("gui/scrolllist.cpp", "\tlastY = last2Y = fastScroll = 0;\n",
              f"\tlastY = last2Y = fastScroll = 0;\n\thfxScrollStarted = false; // {MARK}\n",
              "lastY = last2Y = fastScroll = 0;")
    p.replace("gui/scrolllist.cpp",
              "\tif (abs(scrollingSpeed) < SCROLLING_FLOOR)\n\t\tscrollingSpeed = 0;\n\tHandleScrolling();\n\tmUpdate = 1;\n",
              "\tif (abs(scrollingSpeed) < SCROLLING_FLOOR)\n\t\tscrollingSpeed = 0;\n"
              f"\tint hfxPrevFirst = firstDisplayedItem; // {MARK}\n"
              "\tHandleScrolling();\n"
              "\tif (firstDisplayedItem != hfxPrevFirst && actualItemHeight > 0)\n"
              "\t\tDataManager::ScrollHaptic(abs(oldScrollingSpeed) * 100 / actualItemHeight);\n"
              "\tmUpdate = 1;\n",
              "Update() HandleScrolling")
    p.replace("gui/scrolllist.cpp", "\t\tlastY = last2Y = y;\n",
              f"\t\tlastY = last2Y = y;\n\t\thfxScrollStarted = false; // {MARK}\n",
              "lastY = last2Y = y;")
    p.replace("gui/scrolllist.cpp",
              "\t\tif (hasScroll) {\n\t\t\ty_offset += y - lastY;",
              "\t\tif (hasScroll) {\n"
              f"\t\t\tint hfxDrag = abs(y - lastY); // {MARK}\n"
              "\t\t\tint hfxPrevFirst = firstDisplayedItem;\n"
              "\t\t\tif (!hfxScrollStarted) {\n"
              "\t\t\t\thfxScrollStarted = true;\n"
              "\t\t\t\tDataManager::ScrollHaptic(-1); // tick ringan saat gesture scroll dimulai\n"
              "\t\t\t}\n"
              "\t\t\ty_offset += y - lastY;",
              "TOUCH_DRAG hasScroll")
    p.replace("gui/scrolllist.cpp",
              "\t\t\tlastY = y; // update last touch to the current touch so we can tell how far and what direction we scroll for the next touch event\n\n\t\t\tHandleScrolling();\n",
              "\t\t\tlastY = y; // update last touch to the current touch so we can tell how far and what direction we scroll for the next touch event\n\n\t\t\tHandleScrolling();\n"
              "\t\t\tif (firstDisplayedItem != hfxPrevFirst && actualItemHeight > 0)\n"
              f"\t\t\t\tDataManager::ScrollHaptic(hfxDrag * 100 / actualItemHeight); // {MARK}\n",
              "TOUCH_DRAG HandleScrolling")
    # theme
    # tombol di halaman Vibration dulu (halaman baru juga berisi slider tw_action_vibrate)
    p.replace("gui/theme/common/portrait.xml", VIB_ANCHOR, VIB_BUTTONS, "slider tw_action_vibrate")
    pages = "".join(cat_page(*c) for c in CATS) + test_page()
    p.replace("gui/theme/common/portrait.xml", '\t\t<page name="settings_language">\n',
              pages + '\t\t<page name="settings_language">\n', 'page settings_language')

    if not p.ok:
        warn("bootable/recovery TIDAK diubah (ada pola yang tidak cocok). TWRP tetap dibuild tanpa menu efek.")
        return False
    p.write()
    print(f"twrp_haptic_effects: bootable/recovery -> {len(p.files)} file dipatch (menu Efek Haptic)")
    return True


def patch_qcom_hal(root):
    v = os.path.join(root, "vendor", "qcom", "opensource", "vibrator")
    f = os.path.join(v, "aidl", "Vibrator.cpp")
    marker = os.path.join(v, ".ingres_awinic")
    try:
        with open(f) as fh:
            src = fh.read()
    except OSError:
        warn("vendor/qcom/opensource/vibrator/aidl/Vibrator.cpp tidak ada: HAL qcom tidak dibuild, menu efek tidak akan muncul")
        return False
    if '"awinic_haptic"' not in src:
        old = 'strcmp(name, "qti-haptics")'
        if old not in src:
            warn("Vibrator.cpp: pola strcmp(name, \"qti-haptics\") tidak ketemu, HAL qcom dilewati")
            return False
        src = src.replace(old, old + ' && strcmp(name, "awinic_haptic") && strcmp(name, "aw8697_haptic")', 1)
        with open(f, "w") as fh:
            fh.write(src)
    open(marker, "a").close()
    print("twrp_haptic_effects: HAL qcom -> motor awinic_haptic/aw8697_haptic dikenali (.ingres_awinic)")
    return True


def main():
    if len(sys.argv) != 2:
        print("usage: twrp_haptic_effects.py <root source TWRP>")
        return 1
    root = sys.argv[1].rstrip("/")
    menu = patch_recovery(root)
    hal = patch_qcom_hal(root)
    print("twrp_haptic_effects: Haptic: " + ("menu Efek Haptic" if menu else "getar biasa")
          + " | HAL " + ("qcom (efek)" if hal else "tanpa HAL qcom"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
