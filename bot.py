import telebot

# আপনার টেলিগ্রাম বট টোকেন 🔑
TOKEN = "8655496659:AAEEV-VbJDQpXfVUhHQWvzedzeM0BHL9RGM"

bot = telebot.TeleBot(TOKEN)

# /start বা /hello কমান্ডের প্রতিক্রিয়া 💬
@bot.message_handler(commands=['start', 'hello'])
def send_welcome(message):
    bot.reply_to(message, "হ্যালো! আমি আপনার নতুন টেলিগ্রাম বট।")

# সাধারণ মেসেজের উত্তর (Echo) 🔁
@bot.message_handler(func=lambda msg: True)
def echo_all(message):
    bot.reply_to(message, message.text)

# বটটি সক্রিয় রাখার জন্য ⚙️
bot.infinity_polling()
