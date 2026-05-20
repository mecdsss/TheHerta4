from enum import Enum


class BinDimension(Enum):
    BOTH = 0
    WIDTH = 1
    HEIGHT = 2


class FlippingOption(Enum):
    DISABLED = 0
    ENABLED = 1


class RectWH:
    def __init__(self, w=0, h=0):
        self.w = w
        self.h = h

    def flip(self):
        self.w, self.h = self.h, self.w
        return self

    def area(self):
        return self.w * self.h

    def perimeter(self):
        return 2 * (self.w + self.h)

    def expand_with(self, rect):
        self.w = max(self.w, rect.x + rect.w)
        self.h = max(self.h, rect.y + rect.h)


class RectXYWH:
    def __init__(self, x=0, y=0, w=0, h=0):
        self.x = x
        self.y = y
        self.w = w
        self.h = h

    def area(self):
        return self.w * self.h

    def perimeter(self):
        return 2 * (self.w + self.h)

    def get_wh(self):
        return RectWH(self.w, self.h)


class RectXYWHF(RectXYWH):
    def __init__(self, x=0, y=0, w=0, h=0, flipped=False):
        super().__init__(x, y, w if not flipped else h, h if not flipped else w)
        self.flipped = flipped

    def get_wh(self):
        return RectWH(self.w, self.h)


class CreatedSplits:
    def __init__(self, *spaces):
        self.count = len(spaces)
        self.spaces = list(spaces)
        while len(self.spaces) < 2:
            self.spaces.append(None)

    @staticmethod
    def failed():
        result = CreatedSplits()
        result.count = -1
        return result

    @staticmethod
    def none():
        return CreatedSplits()

    def better_than(self, other):
        return self.count < other.count

    def __bool__(self):
        return self.count != -1


def insert_and_split(image_rect, space_rect):
    free_w = space_rect.w - image_rect.w
    free_h = space_rect.h - image_rect.h

    if free_w < 0 or free_h < 0:
        return CreatedSplits.failed()
    if free_w == 0 and free_h == 0:
        return CreatedSplits.none()
    if free_w > 0 and free_h == 0:
        return CreatedSplits(RectXYWH(space_rect.x + image_rect.w, space_rect.y, free_w, space_rect.h))
    if free_w == 0 and free_h > 0:
        return CreatedSplits(RectXYWH(space_rect.x, space_rect.y + image_rect.h, space_rect.w, free_h))
    if free_w > free_h:
        return CreatedSplits(
            RectXYWH(space_rect.x + image_rect.w, space_rect.y, free_w, space_rect.h),
            RectXYWH(space_rect.x, space_rect.y + image_rect.h, image_rect.w, free_h),
        )
    return CreatedSplits(
        RectXYWH(space_rect.x, space_rect.y + image_rect.h, space_rect.w, free_h),
        RectXYWH(space_rect.x + image_rect.w, space_rect.y, free_w, image_rect.h),
    )


class DefaultEmptySpaces:
    def __init__(self):
        self.empty_spaces = []

    def remove(self, index):
        self.empty_spaces[index] = self.empty_spaces[-1]
        self.empty_spaces.pop()

    def add(self, rect):
        self.empty_spaces.append(rect)
        return True

    def get_count(self):
        return len(self.empty_spaces)

    def reset(self):
        self.empty_spaces.clear()

    def get(self, index):
        return self.empty_spaces[index]


class EmptySpaces:
    def __init__(self, rect):
        self.current_aabb = RectWH()
        self.spaces = DefaultEmptySpaces()
        self.flipping_mode = FlippingOption.ENABLED
        self.allow_flip = True
        self.reset(rect)

    def reset(self, rect):
        self.current_aabb = RectWH()
        self.spaces.reset()
        self.spaces.add(RectXYWH(0, 0, rect.w, rect.h))

    def insert(self, image_rect):
        try_flipping = self.allow_flip and self.flipping_mode == FlippingOption.ENABLED
        for index in range(self.spaces.get_count() - 1, -1, -1):
            candidate_space = self.spaces.get(index)
            normal = insert_and_split(image_rect, candidate_space)
            flipped = None
            if try_flipping:
                flipped = insert_and_split(RectWH(image_rect.w, image_rect.h).flip(), candidate_space)

            if normal and flipped:
                should_flip = flipped.better_than(normal)
                splits = flipped if should_flip else normal
            elif normal:
                should_flip = False
                splits = normal
            elif flipped:
                should_flip = True
                splits = flipped
            else:
                continue

            self.spaces.remove(index)
            for split_index in range(splits.count):
                self.spaces.add(splits.spaces[split_index])

            result = RectXYWHF(candidate_space.x, candidate_space.y, image_rect.w, image_rect.h, should_flip)
            self.current_aabb.expand_with(result)
            return result
        return None


class RectPack2D:
    @staticmethod
    def _try_pack_all_rectangles(rectangles, bin_size):
        rects_copy = [RectXYWHF(0, 0, rect.w, rect.h) for rect in rectangles]
        for index, rect in enumerate(rects_copy):
            rect.material_id = rectangles[index].material_id
        packing_root = EmptySpaces(bin_size)
        for rect in rects_copy:
            inserted_rect = packing_root.insert(rect.get_wh())
            if inserted_rect is None:
                return False, rects_copy
            rect.x = inserted_rect.x
            rect.y = inserted_rect.y
            rect.flipped = getattr(inserted_rect, "flipped", False)
        return True, rects_copy

    def _find_best_bin_size(self, rectangles, max_bin_side, discard_step=1):
        best_bin = RectWH(max_bin_side, max_bin_side)

        def try_with_dimension(dimension, current_best):
            candidate_bin = RectWH(current_best.w, current_best.h)
            if dimension == BinDimension.BOTH:
                candidate_bin.w //= 2
                candidate_bin.h //= 2
                step = candidate_bin.w // 2
            elif dimension == BinDimension.WIDTH:
                candidate_bin.w //= 2
                step = candidate_bin.w // 2
            else:
                candidate_bin.h //= 2
                step = candidate_bin.h // 2

            best_result = None
            while True:
                success, packed_rects = self._try_pack_all_rectangles(rectangles, candidate_bin)
                if success:
                    prev_success = RectWH(candidate_bin.w, candidate_bin.h)
                    best_result = packed_rects
                    if step <= abs(discard_step):
                        return prev_success, best_result
                    if dimension == BinDimension.BOTH:
                        candidate_bin.w -= step
                        candidate_bin.h -= step
                    elif dimension == BinDimension.WIDTH:
                        candidate_bin.w -= step
                    else:
                        candidate_bin.h -= step
                elif dimension == BinDimension.BOTH:
                    candidate_bin.w += step
                    candidate_bin.h += step
                    if candidate_bin.area() > current_best.area():
                        return current_best, best_result
                elif dimension == BinDimension.WIDTH:
                    candidate_bin.w += step
                    if candidate_bin.w > current_best.w:
                        return current_best, best_result
                else:
                    candidate_bin.h += step
                    if candidate_bin.h > current_best.h:
                        return current_best, best_result
                step = max(1, step // 2)

        best_bin, best_packing = try_with_dimension(BinDimension.BOTH, best_bin)
        if best_packing:
            width_bin, width_packing = try_with_dimension(BinDimension.WIDTH, best_bin)
            if width_packing and width_bin.area() < best_bin.area():
                best_bin = width_bin
                best_packing = width_packing
            height_bin, height_packing = try_with_dimension(BinDimension.HEIGHT, best_bin)
            if height_packing and height_bin.area() < best_bin.area():
                best_bin = height_bin
                best_packing = height_packing
        return best_bin, best_packing or []

    def pack(self, images):
        rectangles = []
        for material_id, image_data in images.items():
            width, height = image_data["gfx"]["size"]
            rect = RectXYWHF(0, 0, width, height)
            rect.material_id = material_id
            rectangles.append(rect)

        total_area = sum(rect.area() for rect in rectangles)
        max_dimension = max(max(rect.w, rect.h) for rect in rectangles)
        max_bin_side = min(max(max_dimension * 2, int(total_area ** 0.5 * 1.5)), 20000)

        best_area = float("inf")
        best_packing = None
        strategies = [
            lambda rect: rect.area(),
            lambda rect: rect.perimeter(),
            lambda rect: max(rect.w, rect.h),
            lambda rect: rect.w,
            lambda rect: rect.h,
        ]

        for strategy in strategies:
            sorted_rects = sorted(rectangles, key=strategy, reverse=True)
            bin_size, packed_rects = self._find_best_bin_size(sorted_rects, max_bin_side, discard_step=-4)
            if not packed_rects:
                continue
            area = bin_size.area()
            aspect_ratio = max(bin_size.w / max(bin_size.h, 1), bin_size.h / max(bin_size.w, 1))
            area_with_penalty = area * (1 + ((aspect_ratio - 1) ** 2) * 0.15)
            if area_with_penalty < best_area:
                best_area = area_with_penalty
                best_packing = {
                    rect.material_id: {"x": rect.x, "y": rect.y, "flipped": rect.flipped}
                    for rect in packed_rects
                }

        if best_packing:
            for material_id, pack_data in best_packing.items():
                original_width, original_height = images[material_id]["gfx"]["size"]
                images[material_id]["gfx"]["fit"] = {
                    "x": pack_data["x"],
                    "y": pack_data["y"],
                    "w": original_height if pack_data["flipped"] else original_width,
                    "h": original_width if pack_data["flipped"] else original_height,
                }
        return images
