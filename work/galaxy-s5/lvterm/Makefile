# Makefile for lvterm, the LVGL based Galaxy S5 terminal UI.
#
# The phone has no compiler on it, so this cross compiles for arm-linux-musleabi
# with the vendored zig in scratch-toolchain: the same toolchain that builds
# s5term, fbdump and fbwatch, so every binary on the device comes out of one
# compiler. The result is static and gets copied over as is.
#
# LVGL is compiled straight from its sources. It is a library, not something to
# be generated, and this Makefile is not standing in for a build system: it is
# the tool for turning four hundred and sixty four sources into one binary in
# parallel without running into the argument length and quoting problems that
# doing it by hand does.
#
#   make            build
#   make patch      re-apply the two LVGL fixes to scratch-lvgl/patched
#   make diff       write lvgl-patches.diff against the pristine tree
#   make clean

LVGL   := $(realpath ../scratch-lvgl/patched)
ZIG    := $(realpath ../scratch-toolchain/zig-aarch64-macos-0.16.0/zig)
TARGET := arm-linux-musleabi

BUILD  := /tmp/lvterm-build
OBJDIR := $(BUILD)/obj

# LVGL's own config lookup supports being told the path outright, which is
# tidier than dropping a copy of lv_conf.h into someone else's source tree.
CONF := -DLV_CONF_PATH='"$(realpath lv_conf.h)"'

# LVGL is not warning clean under -Wextra and its warnings are not the point
# here, so the noisy rather than informative ones are named explicitly.
COMMON := -O2 -Wall -Wno-unused-parameter -Wno-sign-compare \
          -Wno-unused-but-set-variable -Wno-missing-field-initializers \
          $(CONF) -I$(LVGL) -I$(LVGL)/src -I$(LVGL)/src/drivers/display/fb \
          -I$(LVGL)/src/drivers/evdev -I$(realpath ..)

CC := $(ZIG) cc -target $(TARGET)

# vpath lets one pattern rule cover the whole LVGL tree: the object path mirrors
# the source path, so there is no name mapping and no per source rule.
vpath %.c $(LVGL)/src

LVGL_SRCS := $(shell find $(LVGL)/src -name '*.c' | sort)
LVGL_OBJS := $(patsubst $(LVGL)/src/%.c,$(OBJDIR)/%.o,$(LVGL_SRCS))
OUR_OBJ   := $(OBJDIR)/lvterm.o
OBJS      := $(LVGL_OBJS) $(OUR_OBJ)

.PHONY: all clean patch diff count

all: $(BUILD)/lvterm
	@echo
	@ls -l $(BUILD)/lvterm
	@shasum -a 256 $(BUILD)/lvterm

$(BUILD)/lvterm: $(OBJS)
	@mkdir -p $(BUILD)
	@echo "linking $(words $(OBJS)) objects"
	@$(CC) -static -o $@ $(OBJS)

# lv_conf.h is a prerequisite of every object, not just the ones that include it
# directly. Almost all of LVGL includes it, and an object that was built against
# a different configuration is a silent miscompile, not a build failure. This
# bit us once: montserrat_28 was switched on in lv_conf.h, the object was not
# rebuilt, and the link failed with an undefined symbol for a font that the
# source file plainly defines.
$(OBJDIR)/%.o: %.c lv_conf.h
	@mkdir -p $(dir $@)
	@$(CC) $(COMMON) -c $< -o $@ 2> $@.log \
	  || { echo "COMPILE FAILED: $<"; cat $@.log; exit 1; }

count:
	@echo "LVGL sources: $(words $(LVGL_SRCS))"
	@echo "total objects: $(words $(OBJS))"

patch:
	python3 apply-lvgl-patches.py $(LVGL)

diff:
	@diff -ru --label pristine --label patched \
	  ../scratch-lvgl/pristine ../scratch-lvgl/patched \
	  > lvgl-patches.diff || true
	@echo "wrote lvgl-patches.diff"
	@grep -c '^[+-]' lvgl-patches.diff | sed 's/^/  changed lines: /'

clean:
	rm -rf $(BUILD)
