# Fura Mini App

Telegram Mini App (ilova ichidagi to'liq sahifali interfeys) — fura egalari uchun
reyslar, xarajatlar va foydani boshqarish. Har bir foydalanuvchi faqat o'zining
ma'lumotlarini ko'radi.

## Loyihada nima bor

```
fura_miniapp/
  main.py          - server (API + botni ishga tushiradi)
  database.py       - ma'lumotlar bazasi (SQLite)
  auth.py           - Telegram foydalanuvchisini tekshirish
  static/index.html - ilovaning ko'rinishi (5 bo'lim: Moshinalar, Reyslar, Xarajatlar, Safarlar, Hisobot)
  requirements.txt  - kerakli kutubxonalar
  .env.example      - sozlamalar namunasi
  Procfile          - hosting uchun ishga tushirish buyrug'i
```

## Muhim tushuncha: nega "faqat mening kompyuterim" yetarli emas

Oddiy `/buyruq`li botdan farqli o'laroq, Mini App ochilishi uchun Telegram unga
**internetda doim ochiq turadigan https-manzil** talab qiladi. Uy kompyuteri
buni ta'minlay olmaydi (routerlar, IP manzil va xavfsizlik devori bunga
to'sqinlik qiladi).

Yaxshi xabar: bu degani kompyuteringiz doim yoniq turishi shart emas.
Kodni bepul/arzon **hosting** xizmatiga (masalan Render.com) joylab qo'yasiz,
u esa doim ishlab turadi — kompyuteringiz o'chiq bo'lsa ham bot ishlayveradi.

## Ishga tushirish uchun kerak bo'ladigan narsalar

1. **Telegram bot tokeni** — @BotFather orqali olinadi
2. **Hosting** — kodni joylashtirib, doim ishlab turadigan joy (masalan Render.com, bepul)
3. **GitHub akkaunt** — kodni hostingga yuklash uchun oraliq bosqich (bepul, faqat email kerak)

Bularning barchasini keyingi bosqichda birga, screenshot darajasida
qadam-baqadam sozlaymiz — hoziroq bezovta bo'lishingiz shart emas.

## Bot qanday ishlaydi (funksional tavsif)

### 🚛 Moshinalar
- Yangi fura 3 qadamda qo'shiladi: davlat raqami → markasi → necha kub
- Bitta akkauntga 5 tagacha mashina
- Har bir davlat raqami butun botda faqat bir marta qo'shilishi mumkin
  (`01 A 123 BC`, `01a123bc`, kirill harflarida yozilgani — hammasi bitta raqam)
- Raqam boshqa akkauntda bo'lsa, "allaqachon qo'shilgan" deb chiqadi va egasiga
  kuzatish so'rovini yuborish mumkin. Egasi botda "Ruxsat berish / Rad etish"
  tugmali xabar oladi (so'rov ilovadagi "Moshinalar" bo'limida ham ko'rinadi)
- Ruxsat olgan akkaunt mashinaning xarajatlari, reyslari va foydasini ko'radi,
  lekin hech narsani o'zgartira olmaydi. Egasi kuzatuvchini istalgan payt olib
  tashlashi mumkin
- Har bir furaga ta'mirlash/balon kabi xarajatlarni yozib borish
- Har bir fura kartasida jami ta'mirlash summasi ko'rinadi
- Fura o'chirilsa, uning barcha reyslari ham birga o'chadi

### 🧭 Reyslar
- Yangi reys boshlash (fura tanlanadi)
- Har bir furada bir vaqtning o'zida faqat bitta faol reys bo'lishi mumkin
  (bir nechta fura bo'lsa, har biri o'z reysida yurishi mumkin)
- Har bir reys kartasida: daromad, xarajat, sof foyda
- Reysni bosib, uning to'liq tafsilotini (safarlar + xarajatlar) ko'rish mumkin
- "Reysni yakunlash" tugmasi bilan reysni yopish

### 💸 Xarajatlar
- Joriy (faol) reys uchun xarajat qo'shish: Yoqilg'i / Yo'l haqi / Ovqatlanish / Boshqa
- Jami xarajat yuqorida katta raqam bilan ko'rinadi

### 📍 Safarlar
- Joriy reys davomida bir nechta yuk yo'nalishini qo'shish (qayerdan — qayerga — necha pulga)
- Jami daromad yuqorida katta raqam bilan ko'rinadi
- Safar qo'shishda yo'nalish yozilishi bilan pastda **bozordagi o'rtacha narx** chiqadi
  (masalan Andijon → Toshkent: oxirgi 7 kunda e'lon qilingan yuklar o'rtachasi) va
  kiritilgan narx o'rtachadan necha foiz yuqori/pastligi ko'rsatiladi
- **"Qimmat yuklarni botga yuborish"** belgilansa, manzildan (masalan Toshkentdan)
  chiqadigan, narxi o'z yo'nalishi o'rtachasiga teng yoki undan yuqori yangi yuklar
  bot chatiga yuborib turiladi. Ixtiyoriy "Keyingi yo'nalish" maydoniga shahar
  (Moskva) yoki davlat (Rossiya) yozilsa, faqat o'sha tomonga ketadigan yuklar keladi
- Yuklar mashina hajmiga (kub) qarab tanlanadi: e'londa ko'rsatilgan hajm mashinaga
  sig'masa yuborilmaydi
- Bitta reysda bir vaqtda bitta safar uchun yuboriladi (oxirgi yoqilgani); reys
  yakunlanganda yoki botdagi "To'xtatish" tugmasi bosilganda to'xtaydi

### 📊 Hisobot
- Umumiy sof foyda/zarar
- Oylar kesimida daromad/xarajat taqqoslash
- Har bir fura bo'yicha alohida foyda/zarar

## Ma'lumotlar qayerda saqlanadi

Barcha ma'lumot SQLite faylida (`fura.db`) saqlanadi. Joyini `DB_PATH` sozlamasi
bilan o'zgartirish mumkin.

**Diqqat:** Render.com'ning bepul tarifida disk vaqtinchalik — har bir deploy yoki
qayta ishga tushishda fayl o'chib ketadi va **barcha ma'lumot yo'qoladi**. Bundan
tashqari, bepul server 15 daqiqa so'rov kelmasa uxlab qoladi va bot `/start`ga
javob bermay qo'yadi. Ma'lumot saqlanib qolishi uchun doimiy disk (Render'da
"Persistent Disk", masalan `/var/data`) ulab, `DB_PATH=/var/data/fura.db` qilib
qo'ying.

## Bozor narxlari: Qimmatyuklar bazasi

O'rtacha narx va qimmat yuklar [Qimmatyuklar](https://github.com/ilyos21code/qimmatyuklar)
(`yuk_top.py`) Telegram yuk kanallaridan yig'adigan `yuk.db` bazasidan olinadi. Fura
bot bu faylni **faqat o'qiydi** (SQLite `mode=ro`), unga hech narsa yozmaydi. Ikkala
bot bitta serverda ishlaydi; ulash uchun serverda bir marta:

```
sudo /opt/fura-bot2/app/deploy/connect-yuk-db.sh            # yuk.db ni o'zi topadi
sudo /opt/fura-bot2/app/deploy/connect-yuk-db.sh /yo'l/yuk.db
```

Skript `furabot2` foydalanuvchisiga faqat shu faylni o'qish huquqini beradi,
`YUK_DB_PATH` ni systemd sozlamasiga yozadi va botni qayta ishga tushiradi.
`YUK_DB_PATH` berilmasa yoki fayl o'qilmasa, bu funksiyalar ilovada shunchaki
ko'rinmaydi — qolgan hamma narsa odatdagidek ishlaydi.

Shahar nomlari (`Ташкент`, `tashkent`, `Toshkentga` → Toshkent) Qimmatyuklar bilan
bir xil qoidada tanilishi uchun `geo_data.json` o'sha loyihadan olingan nusxa.
U yerda yangi shahar qo'shilsa, bu faylni ham yangilab qo'yish kerak.

## Lokal sinov (ixtiyoriy, dasturchilar uchun)

Agar birov (masalan men) buni terminalda sinab ko'rmoqchi bo'lsa:

```
pip install -r requirements.txt
export BOT_TOKEN="tokeningiz"
export DEV_MODE=1
python main.py
```

`DEV_MODE=1` bo'lganda, Telegram ichida ochmasdan ham `http://localhost:8000`
manzilida ilovani brauzerda ko'rish mumkin (test uchun soxta foydalanuvchi bilan).

## Keyingi qadam

Endi buni haqiqiy Telegram botiga ulash va internetga chiqarish kerak. Bu:
1. @BotFather orqali bot va token olish
2. Render.com'da bepul hosting ochish
3. Kodni GitHub orqali hostingga yuklash
4. Bot tokeni va hosting manzilini bir-biriga bog'lash

Bu bosqichlarni birga, screenshot darajasida qadam-baqadam bajaramiz.
