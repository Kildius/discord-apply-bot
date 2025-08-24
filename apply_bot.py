# apply_bot.py
# Lone Samurai — заявка в команду: привет-сообщение → выбор роли → анкета → тикет → модерация

import os
import asyncio
import logging
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands

# -----------------------------
# НАСТРОЙКИ / КОНСТАНТЫ
# -----------------------------

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ls-apply")

TOKEN = os.getenv("DISCORD_TOKEN")
WELCOME_CHANNEL_ID = int(os.getenv("WELCOME_CHANNEL_ID", "0"))  # канал #анкета
TICKETS_CATEGORY_ID = int(os.getenv("TICKETS_CATEGORY_ID", "0"))  # категория "Вступай-В-Команду"

# Роли модераторов, у которых есть доступ к тикетам и кнопкам «Одобрить/Отклонить»
MOD_ROLE_IDS: List[int] = []
_raw_mods = os.getenv("MOD_ROLE_IDS", "").strip()
if _raw_mods:
    for x in _raw_mods.split(","):
        x = x.strip()
        if x.isdigit():
            MOD_ROLE_IDS.append(int(x))

# Маппинг ролей кандидатов (ваши готовые ID)
ROLES = {
    "Клинер": 1225460519712985140,
    "Тайпер": 1225462096318169198,
    "Редактор": 1225462307694448781,
    "Переводчик-EN": 1225460206083903549,
    "Переводчик-KR": 1409098606157365279,
}

# Техническая метка в привет-сообщении, чтобы не дублировать его
WELCOME_TAG = "LS_APPLY_WELCOME"

# -----------------------------
# БОТ
# -----------------------------

intents = discord.Intents.default()
intents.members = True  # нужно для выдачи ролей/прав
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


# -----------------------------
# УТИЛИТЫ
# -----------------------------

def is_mod(user: discord.Member) -> bool:
    return any(r.id in MOD_ROLE_IDS for r in user.roles)


async def get_welcome_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    return guild.get_channel(WELCOME_CHANNEL_ID) or await bot.fetch_channel(WELCOME_CHANNEL_ID)


async def get_tickets_category(guild: discord.Guild) -> Optional[discord.CategoryChannel]:
    cat = discord.utils.get(guild.categories, id=TICKETS_CATEGORY_ID)
    if not cat:
        try:
            cat = await bot.fetch_channel(TICKETS_CATEGORY_ID)  # тип будет CategoryChannel
        except Exception:
            cat = None
    return cat  # type: ignore


async def ensure_welcome_message(guild: discord.Guild):
    """
    Отправляет (или обновляет) привет-сообщение с кнопкой в #анкета.
    """
    ch = await get_welcome_channel(guild)
    if not isinstance(ch, discord.TextChannel):
        log.warning("WELCOME_CHANNEL_ID указывает не на текстовый канал.")
        return

    # Ищем наше сообщение по метке WELCOME_TAG
    async for m in ch.history(limit=50):
        if m.author == bot.user and WELCOME_TAG in "".join([m.content] + [e.description if isinstance(e, discord.Embed) else "" for e in m.embeds]):
            # Обновим view, если нужно
            try:
                await m.edit(view=WelcomeView())
            except Exception:
                pass
            return

    # Не нашли — шлём новое
    embed = discord.Embed(
        title=WELCOME_TAG,
        description=(
            "👋 **Здравствуйте!** Хотите стать членом команды **Lone Samurai**?\n\n"
            "Нажмите кнопку ниже, чтобы заполнить анкету. "
            "После отправки для вас **создастся приватный тикет-канал** с модераторами."
        ),
        color=discord.Color.gold(),
    )
    await ch.send(embed=embed, view=WelcomeView())


async def create_ticket_channel(guild: discord.Guild, member: discord.Member) -> discord.TextChannel:
    """
    Создаёт приватный текстовый канал в категории тикетов.
    Права: видит только кандидат, мод-роли и бот.
    """
    category = await get_tickets_category(guild)
    if not isinstance(category, discord.CategoryChannel):
        raise RuntimeError("Категория для тикетов не найдена. Проверьте TICKETS_CATEGORY_ID.")

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
    }
    for rid in MOD_ROLE_IDS:
        role = guild.get_role(rid)
        if role:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    name = f"заявка-{member.name}".lower().replace(" ", "-")
    channel = await guild.create_text_channel(
        name=name[:90],
        category=category,
        overwrites=overwrites,
        reason=f"Тикет заявки для {member} ({member.id})",
    )
    return channel


# -----------------------------
# UI: ВЫБОР РОЛИ → МОДАЛКА
# -----------------------------

class RoleSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=label) for label in ROLES.keys()]
        super().__init__(placeholder="Выберите роль", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        role_name = self.values[0]
        await interaction.response.send_modal(ApplicationModal(role_name))


class RoleSelectView(discord.ui.View):
    def __init__(self, *, timeout: float | None = 180):
        super().__init__(timeout=timeout)
        self.add_item(RoleSelect())


class ApplicationModal(discord.ui.Modal, title="Анкета кандидата"):
    def __init__(self, role_name: str):
        super().__init__()
        self.role_name = role_name

        self.nick = discord.ui.TextInput(label="Ник (в игре/работе)*", required=True, max_length=64)
        self.age = discord.ui.TextInput(label="Возраст*", required=True, max_length=3, placeholder="например: 21")
        self.location = discord.ui.TextInput(label="Город / страна*", required=True, max_length=64)
        self.tz = discord.ui.TextInput(label="Часовой пояс относительно МСК*", required=True, placeholder="например: +3")
        self.about = discord.ui.TextInput(label="О себе / опыт", required=False, style=discord.TextStyle.paragraph, max_length=500)

        # В модалке допускается максимум 5 инпутов
        self.add_item(self.nick)
        self.add_item(self.age)
        self.add_item(self.location)
        self.add_item(self.tz)
        self.add_item(self.about)

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Гильдия не найдена.", ephemeral=True)

        member: discord.Member = interaction.user

        # Создаём тикет-канал
        try:
            ticket = await create_ticket_channel(interaction.guild, member)
        except Exception as e:
            log.exception("Не удалось создать тикет-канал")
            return await interaction.response.send_message(f"Ошибка: {e}", ephemeral=True)

        # Сообщение в тикете
        role_id = ROLES.get(self.role_name)
        role_mention = f"<@&{role_id}>" if role_id else self.role_name

        embed = discord.Embed(title="🆕 Новая заявка", color=discord.Color.blurple())
        embed.add_field(name="Кандидат", value=f"{member.mention} (`{member.id}`)", inline=False)
        embed.add_field(name="Роль", value=role_mention, inline=True)
        embed.add_field(name="Ник", value=str(self.nick), inline=True)
        embed.add_field(name="Возраст", value=str(self.age), inline=True)
        embed.add_field(name="Город/страна", value=str(self.location), inline=True)
        embed.add_field(name="Часовой пояс (к МСК)", value=str(self.tz), inline=True)
        if str(self.about).strip():
            embed.add_field(name="О себе / опыт", value=str(self.about), inline=False)

        await ticket.send(
            content="Заявка создана. Ожидайте решения модерации.",
            embed=embed,
            view=ModeratorDecisionView(applicant_id=member.id, role_id=role_id)
        )

        # Ответ пользователю
        await interaction.response.send_message(
            "Заявка отправлена! Для вас создан приватный канал с модераторами.", ephemeral=True
        )


class WelcomeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Заполнить анкету", style=discord.ButtonStyle.primary, custom_id="ls_apply_start")
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Выберите роль, на которую подаёте:", view=RoleSelectView(), ephemeral=True
        )


# -----------------------------
# UI: МОДЕРАЦИЯ ТИКЕТА
# -----------------------------

class CloseByApplicantView(discord.ui.View):
    def __init__(self, *, applicant_id: int):
        super().__init__(timeout=600)
        self.applicant_id = applicant_id

    @discord.ui.button(label="Аригато", style=discord.ButtonStyle.secondary, emoji="🫶")
    async def thanks(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member):
            return
        if interaction.user.id != self.applicant_id:
            return await interaction.response.send_message("Эта кнопка — для автора тикета.", ephemeral=True)
        await interaction.response.send_message("Закрываю тикет…", ephemeral=True)
        await asyncio.sleep(1)
        try:
            await interaction.channel.delete(reason="Закрыто пользователем (Аригато)")
        except Exception:
            pass


class ModeratorDecisionView(discord.ui.View):
    def __init__(self, *, applicant_id: int, role_id: Optional[int]):
        super().__init__(timeout=None)
        self.applicant_id = applicant_id
        self.role_id = role_id

    async def _get_applicant(self, guild: discord.Guild) -> Optional[discord.Member]:
        try:
            return await guild.fetch_member(self.applicant_id)
        except Exception:
            return guild.get_member(self.applicant_id)

    async def _mod_check(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            return False
        if is_mod(interaction.user):
            return True
        await interaction.response.send_message("Недостаточно прав.", ephemeral=True)
        return False

    @discord.ui.button(label="Одобрить", style=discord.ButtonStyle.success, emoji="✅", custom_id="ls_decide_approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._mod_check(interaction):
            return
        guild = interaction.guild
        if not guild:
            return
        applicant = await self._get_applicant(guild)
        if not applicant:
            return await interaction.response.send_message("Кандидат не найден.", ephemeral=True)

        # Выдаём роль (если задана)
        if self.role_id:
            role = guild.get_role(self.role_id)
            if role:
                try:
                    await applicant.add_roles(role, reason="Заявка одобрена")
                except Exception as e:
                    log.warning(f"Не выдалась роль: {e}")

        await interaction.response.send_message(
            content=f"🎉 {applicant.mention}, поздравляем! Вы приняты. Добро пожаловать в команду.",
            view=CloseByApplicantView(applicant_id=applicant.id)
        )

    @discord.ui.button(label="Отклонить", style=discord.ButtonStyle.danger, emoji="❌", custom_id="ls_decide_deny")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._mod_check(interaction):
            return
        guild = interaction.guild
        if not guild:
            return
        applicant = await self._get_applicant(guild)
        if not applicant:
            return await interaction.response.send_message("Кандидат не найден.", ephemeral=True)

        await interaction.response.send_message(
            content=f"{applicant.mention}, спасибо за интерес. "
                    f"Но пока **идти путём самурая рановато**. Тикет можно закрыть.",
            view=CloseByApplicantView(applicant_id=applicant.id)
        )

    @discord.ui.button(label="Закрыть тикет", style=discord.ButtonStyle.secondary, emoji="🔒", custom_id="ls_decide_close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._mod_check(interaction):
            return
        await interaction.response.send_message("Тикет закрывается…", ephemeral=True)
        await asyncio.sleep(1)
        try:
            await interaction.channel.delete(reason="Закрыто модератором")
        except Exception:
            pass


# -----------------------------
# СЛЭШ-КОМАНДЫ СЕРВИСНЫЕ
# -----------------------------

@tree.command(name="ping", description="Проверка бота")
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!", ephemeral=True)


@tree.command(name="resync", description="Пересинхронизировать слэш-команды (только для модов)")
async def resync_cmd(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not is_mod(interaction.user):
        return await interaction.response.send_message("Недостаточно прав.", ephemeral=True)
    await tree.sync(guild=interaction.guild)
    await interaction.response.send_message("Команды пересинхронизированы.", ephemeral=True)


@tree.command(name="setup_welcome", description="Переотправить привет-сообщение в #анкета (только для модов)")
async def setup_welcome_cmd(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not is_mod(interaction.user):
        return await interaction.response.send_message("Недостаточно прав.", ephemeral=True)
    if not interaction.guild:
        return await interaction.response.send_message("Гильдия не найдена.", ephemeral=True)
    await ensure_welcome_message(interaction.guild)
    await interaction.response.send_message("Готово.", ephemeral=True)


# -----------------------------
# ЖИЗНЕННЫЙ ЦИКЛ
# -----------------------------

@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "—")
    # Синхронизируем команды для всех гильдий, где есть бот
    for guild in bot.guilds:
        try:
            await tree.sync(guild=guild)
        except Exception as e:
            log.warning(f"Не удалось sync для {guild.name}: {e}")
        try:
            await ensure_welcome_message(guild)
        except Exception as e:
            log.warning(f"Не удалось отправить привет в {guild.name}: {e}")


# -----------------------------
# ЗАПУСК
# -----------------------------

def _check_env():
    miss = []
    if not TOKEN:
        miss.append("DISCORD_TOKEN")
    if not WELCOME_CHANNEL_ID:
        miss.append("WELCOME_CHANNEL_ID")
    if not TICKETS_CATEGORY_ID:
        miss.append("TICKETS_CATEGORY_ID")
    if not MOD_ROLE_IDS:
        miss.append("MOD_ROLE_IDS")
    if miss:
        raise SystemExit(f"[FATAL] Missing environment variable(s): {', '.join(miss)}")

if __name__ == "__main__":
    _check_env()
    bot.run(TOKEN)
