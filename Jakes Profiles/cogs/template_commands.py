import asyncio
import string
import uuid
import typing
import collections

import discord
from discord.ext import commands
import voxelbotutils as utils
import asyncpg

from cogs import utils as localutils


class ProfileTemplates(utils.Cog):

    TICK_EMOJI = "<:tick_yes:596096897995899097>"
    CROSS_EMOJI = "<:cross_no:596096897769275402>"

    NUMBERS_EMOJI = "\U00000031\U000020e3"
    LETTERS_EMOJI = "\U0001F170"
    PICTURE_EMOJI = "\U0001f5bc"

    def __init__(self, bot:utils.Bot):
        super().__init__(bot)
        self.template_editing_locks: typing.Dict[int, asyncio.Lock] = collections.defaultdict(asyncio.Lock)  # guild_id: asyncio.Lock

    @staticmethod
    def is_valid_template_name(template_name):
        return len([i for i in template_name if i not in string.ascii_letters + string.digits]) == 0

    @utils.command()
    @commands.bot_has_permissions(send_messages=True)
    @commands.guild_only()
    async def templates(self, ctx:utils.Context, guild_id:int=None):
        """
        Lists the templates that have been created for this server.
        """

        # See if they're allowed to get from another guild ID
        if guild_id is not None and guild_id != ctx.guild.id and self.bot.config.get('bot_support_role_id') not in ctx.author._roles:
            raise commands.MissingRole("Bot Support Team")

        # Grab the templates
        async with self.bot.database() as db:
            templates = await db(
                """SELECT template.template_id, template.name, COUNT(created_profile.*) FROM template
                LEFT JOIN created_profile ON template.template_id=created_profile.template_id
                WHERE guild_id=$1 GROUP BY template.template_id""",
                guild_id or ctx.guild.id
            )

        if not templates:
            return await ctx.send("There are no created templates for this guild.")
        return await ctx.send('\n'.join([f"**{row['name']}** (`{row['template_id']}`, `{row['count']}` created profiles)" for row in templates]))

    @utils.command(aliases=['describe'])
    @commands.bot_has_permissions(send_messages=True, embed_links=True)
    @commands.guild_only()
    async def describetemplate(self, ctx:utils.Context, template:localutils.Template, brief:bool=True):
        """
        Describe a template and its fields.
        """

        embed = template.build_embed(self.bot, brief=brief)
        async with self.bot.database() as db:
            user_profiles = await template.fetch_all_profiles(db, fetch_filled_fields=False)
        embed.description += f"\nCurrently there are **{len(user_profiles)}** created profiles for this template."
        return await ctx.send(embed=embed)

    async def purge_message_list(self, channel:discord.TextChannel, message_list:typing.List[discord.Message]):
        """
        Delete a list of messages from the channel.
        """

        await channel.purge(check=lambda m: m.id in [i.id for i in message_list], bulk=channel.permissions_for(channel.guild.me).manage_messages)
        message_list.clear()

    @utils.command()
    @commands.has_guild_permissions(manage_roles=True)
    @commands.bot_has_permissions(send_messages=True, external_emojis=True, add_reactions=True, manage_messages=True)
    @commands.guild_only()
    async def edittemplate(self, ctx:utils.Context, template:localutils.Template):
        """
        Edits a template for your guild.
        """

        # See if they're already editing that template
        if self.template_editing_locks[ctx.guild.id].locked():
            return await ctx.send("You're already editing a template.")

        # See if they're bot support
        is_bot_support = False
        try:
            await utils.checks.is_bot_support().predicate(ctx)
            is_bot_support = True
        except commands.CommandError:
            pass

        # Grab the template edit lock
        async with self.template_editing_locks[ctx.guild.id]:

            # Get the template fields
            async with self.bot.database() as db:
                await template.fetch_fields(db)
                guild_settings_rows = await db("SELECT * FROM guild_settings WHERE guild_id=$1 OR guild_id=0 ORDER BY guild_id DESC", ctx.guild.id)
            guild_settings = guild_settings_rows[0]

            # Set up our initial vars so we can edit them later
            template_display_edit_message = await ctx.send("Loading template...")
            template_options_text = (
                "**Select the emoji next to the item you want to edit:**\n"
                "1\u20e3 Template name\n"
                "2\u20e3 Verification channel (where profiles are sent to be verified by staff)\n"
                "3\u20e3 Archive channel (where profiles are sent once verified)\n"
                "4\u20e3 Set a role to be given to users upon completing a profile\n"
                "5\u20e3 Template fields/questions\n"
                "6\u20e3 Maximum profile count per user\n"
            )
            if is_bot_support:
                template_options_text += "7\u20e3 Maximum field count\n"
            template_options_edit_message = await ctx.send(template_options_text)
            messages_to_delete = []
            should_edit = True
            should_add_reactions = True

            # Start our edit loop
            while True:

                # Ask what they want to edit
                if should_edit:
                    try:
                        await template_display_edit_message.edit(
                            content=None,
                            embed=template.build_embed(self.bot, brief=True),
                            allowed_mentions=discord.AllowedMentions(roles=False),
                        )
                    except discord.HTTPException:
                        return
                    should_edit = False

                # Add reactions if there aren't any
                valid_emoji = [
                    "1\N{COMBINING ENCLOSING KEYCAP}", "2\N{COMBINING ENCLOSING KEYCAP}", "3\N{COMBINING ENCLOSING KEYCAP}",
                    "4\N{COMBINING ENCLOSING KEYCAP}", "5\N{COMBINING ENCLOSING KEYCAP}", "6\N{COMBINING ENCLOSING KEYCAP}",
                ]
                if is_bot_support:
                    valid_emoji.append("7\N{COMBINING ENCLOSING KEYCAP}")
                valid_emoji.append(self.TICK_EMOJI)
                if should_add_reactions:
                    for e in valid_emoji:
                        try:
                            await template_options_edit_message.add_reaction(e)
                        except discord.HTTPException:
                            try:
                                await template_display_edit_message.delete()
                                await template_options_edit_message.edit(content="I'm unable to add reactions to my messages.")
                            except discord.HTTPException:
                                pass
                            return
                    should_add_reactions = False

                # Wait for a response
                try:
                    check = lambda p: p.user_id == ctx.author.id and p.message_id == template_options_edit_message.id and str(p.emoji) in valid_emoji
                    payload = await self.bot.wait_for("raw_reaction_add", check=check, timeout=120)
                    reaction = str(payload.emoji)
                except asyncio.TimeoutError:
                    try:
                        return await ctx.send("Timed out waiting for edit response.")
                    except discord.HTTPException:
                        return

                # See what they reacted with
                try:
                    available_reactions = {
                        "1\N{COMBINING ENCLOSING KEYCAP}": ('name', str),
                        "2\N{COMBINING ENCLOSING KEYCAP}": ('verification_channel_id', commands.TextChannelConverter()),
                        "3\N{COMBINING ENCLOSING KEYCAP}": ('archive_channel_id', commands.TextChannelConverter()),
                        "4\N{COMBINING ENCLOSING KEYCAP}": ('role_id', commands.RoleConverter()),
                        "5\N{COMBINING ENCLOSING KEYCAP}": (None, self.edit_field(ctx, template, guild_settings, is_bot_support)),
                        "6\N{COMBINING ENCLOSING KEYCAP}": ('max_profile_count', int),
                        "7\N{COMBINING ENCLOSING KEYCAP}": ('max_field_count', int),
                        self.TICK_EMOJI: None,
                    }
                    attr, converter = available_reactions[reaction]
                except TypeError:
                    break
                await template_options_edit_message.remove_reaction(reaction, ctx.author)

                # If they want to edit a field, we go through this section
                if attr is None:
                    fields_have_changed = await converter
                    if fields_have_changed is None:
                        pass
                    if fields_have_changed:
                        async with self.bot.database() as db:
                            await template.fetch_fields(db)
                        should_edit = True
                    continue

                # Ask what they want to set things to
                if isinstance(converter, commands.Converter):
                    v = await ctx.send(f"What do you want to set the template's **{' '.join(attr.split('_')[:-1])}** to? You can give a name, a ping, or an ID, or say `continue` to set the value to null. " + ("Note that any current pending profiles will _not_ be able to be approved after moving the channel" if attr == 'verification_channel_id' else ''))
                else:
                    v = await ctx.send(f"What do you want to set the template's **{attr.replace('_', ' ')}** to?")
                messages_to_delete.append(v)
                try:
                    value_message = await self.bot.wait_for("message", check=lambda m: m.author.id == ctx.author.id and m.channel.id == ctx.channel.id, timeout=120)
                except asyncio.TimeoutError:
                    try:
                        return await ctx.send("Timed out waiting for edit response.")
                    except discord.HTTPException:
                        return
                messages_to_delete.append(value_message)

                # Convert the response
                try:
                    converted = str((await converter.convert(ctx, value_message.content)).id)

                # The converter failed
                except commands.BadArgument:

                    # They want to set it to none
                    if value_message.content == "continue":
                        converted = None

                    # They either gave a command or just something invalid
                    else:
                        is_command, is_valid_command = localutils.CommandProcessor.get_is_command(value_message.content)
                        if is_command and is_valid_command:
                            converted = value_message.content
                        else:
                            self.bot.loop.create_task(self.purge_message_list(ctx.channel, messages_to_delete))
                            continue

                # It isn't a converter object
                except AttributeError:
                    try:
                        converted = converter(value_message.content)
                    except ValueError:
                        self.bot.loop.create_task(self.purge_message_list(ctx.channel, messages_to_delete))
                        continue

                # Delete the messages we don't need any more
                self.bot.loop.create_task(self.purge_message_list(ctx.channel, messages_to_delete))

                # Validate if they provided a new name
                if attr == 'name':
                    async with self.bot.database() as db:
                        name_in_use = await db(
                            """SELECT * FROM template WHERE guild_id=$1 AND LOWER(name)=LOWER($2)
                            AND template_id<>$3""",
                            ctx.guild.id, converted, template.template_id,
                        )
                        if name_in_use:
                            await ctx.send("That template name is already in use.", delete_after=3)
                            continue
                    if 30 < len(converted) < 1:
                        await ctx.send("That template name is invalid - not within 1 and 30 characters in length.", delete_after=3)
                        continue

                # Validate profile count
                if attr == 'max_profile_count':
                    if is_bot_support:
                        pass
                    else:
                        original_converted = converted
                        converted = max([min([converted, guild_settings['max_template_profile_count']]), 0])
                        if original_converted > converted:
                            await ctx.send(f"Your max profile count has been set to **{guild_settings['max_template_profile_count']}** instead of **{original_converted}**.", delete_after=3)

                # Validate field count
                if attr == 'max_field_count':
                    if is_bot_support:
                        pass
                    else:
                        original_converted = converted
                        converted = max([min([converted, guild_settings['max_template_field_count']]), 0])
                        if original_converted > converted:
                            await ctx.send(f"Your max field count has been set to **{guild_settings['max_template_field_count']}** instead of **{original_converted}**.", delete_after=3)

                # Store our new shit
                setattr(template, attr, converted)
                async with self.bot.database() as db:
                    await db("UPDATE template SET {0}=$1 WHERE template_id=$2".format(attr), converted, template.template_id)
                should_edit = True

        # Tell them it's done
        try:
            await template_options_edit_message.delete()
        except discord.HTTPException:
            pass
        await ctx.send(
            (
                f"Finished editing template. Users can create profiles with `{ctx.clean_prefix}set{template.name.lower()}`, "
                f"edit with `{ctx.clean_prefix}edit{template.name.lower()}`, and show them with `{ctx.clean_prefix}get{template.name.lower()}`."
            )
        )

    async def edit_field(self, ctx:utils.Context, template:localutils.Template, guild_settings:dict, is_bot_support:bool) -> bool:
        """
        Talk the user through editing a field of a template.
        Returns whether or not the template display needs to be updated.
        """

        # Ask which index they want to edit
        if len(template.fields) == 0:
            ask_field_edit_message: discord.Message = await ctx.send("Now talking you through creating a new field.")
        elif len(template.fields) >= max([guild_settings['max_template_field_count'], template.max_field_count]) and not is_bot_support:
            ask_field_edit_message: discord.Message = await ctx.send("What is the index of the field you want to edit?")
        else:
            ask_field_edit_message: discord.Message = await ctx.send("What is the index of the field you want to edit? If you want to add a *new* field, type **new**.")
        messages_to_delete = [ask_field_edit_message]

        # Start our infinite loop
        while True:

            # Wait for them to say which field they want to edit
            if len(template.fields) > 0:
                try:
                    field_index_message: discord.Message = await self.bot.wait_for("message", check=lambda m: m.author.id == ctx.author.id and m.channel.id == ctx.channel.id, timeout=120)
                    messages_to_delete.append(field_index_message)
                except asyncio.TimeoutError:
                    try:
                        await ctx.send("Timed out waiting for field index.")
                    except discord.HTTPException:
                        pass
                    return None

            # Grab the field they want to edit
            try:
                if len(template.fields) == 0:
                    raise ValueError()
                field_index: int = int(field_index_message.content.lstrip('#'))
                field_to_edit: localutils.Field = [i for i in template.fields.values() if i.index == field_index and i.deleted is False][0]
                break

            # They either gave an invalid number or want to make a new field
            except (ValueError, IndexError):

                # They want to create a new field
                if len(template.fields) == 0 or field_index_message.content.lower() == "new":
                    if len(template.fields) < max([guild_settings['max_template_field_count'], template.max_field_count]) or is_bot_support:
                        image_field_exists: bool = any([i for i in template.fields.values() if isinstance(i.field_type, localutils.ImageField)])
                        self.bot.loop.create_task(self.purge_message_list(ctx.channel, messages_to_delete))
                        field: localutils.Field = await self.create_new_field(
                            ctx=ctx,
                            template=template,
                            index=len(template.all_fields),
                            image_set=image_field_exists,
                            prompt_for_creation=False,
                            delete_messages=True
                        )
                        if field is None:
                            return None
                        async with self.bot.database() as db:
                            try:
                                await db(
                                    """INSERT INTO field (field_id, name, index, prompt, timeout, field_type, optional, template_id)
                                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
                                    field.field_id, field.name, field.index, field.prompt, field.timeout, field.field_type.name, field.optional, field.template_id
                                )
                            except asyncpg.ForeignKeyViolationError:
                                # The template was deleted while it was being edited
                                return True
                        return True

                    # They want a new field but they're at the max
                    v = await ctx.send("You're already at the maximum number of fields for this template - please provide a field index to edit.")
                    messages_to_delete.append(v)
                    continue

                # If they just messed up the field creation
                else:
                    v = await ctx.send("That isn't a valid index number - please provide another.")
                    messages_to_delete.append(v)
                    continue

        # Ask what part of it they want to edit
        attribute_message: discord.Message = await ctx.send(
            (
                f"Editing the field **{field_to_edit.name}**. Which part would you like to edit?\n"
                "1\N{COMBINING ENCLOSING KEYCAP} Field name\n"
                "2\N{COMBINING ENCLOSING KEYCAP} Field prompt\n"
                "3\N{COMBINING ENCLOSING KEYCAP} Whether or not the field is optional\n"
                "4\N{COMBINING ENCLOSING KEYCAP} Field type\n"
                "5\N{COMBINING ENCLOSING KEYCAP} Delete field entierly\n"
            ), allowed_mentions=discord.AllowedMentions(users=False, roles=False, everyone=False)
        )
        messages_to_delete.append(attribute_message)
        valid_emoji = [
            "1\N{COMBINING ENCLOSING KEYCAP}", "2\N{COMBINING ENCLOSING KEYCAP}",
            "3\N{COMBINING ENCLOSING KEYCAP}", "4\N{COMBINING ENCLOSING KEYCAP}",
            "5\N{COMBINING ENCLOSING KEYCAP}", self.CROSS_EMOJI
        ]
        for e in valid_emoji:
            await attribute_message.add_reaction(e)

        # Wait for a response
        try:
            check = lambda p: p.user_id == ctx.author.id and p.message_id == attribute_message.id and str(p.emoji) in valid_emoji
            reaction = await self.bot.wait_for("raw_reaction_add", check=check, timeout=120)
            emoji = str(reaction.emoji)
        except asyncio.TimeoutError:
            try:
                await ctx.send("Timed out waiting for field attribute.")
            except discord.HTTPException:
                pass
            return None

        # Let's set up our validity converters for each of the fields
        def name_validity_checker(given_value):
            if len(given_value) > 256 or len(given_value) <= 0:
                return "Your given field name is too long. Please provide another."
            return True

        # See what they reacted with
        try:
            available_reactions = {
                "1\N{COMBINING ENCLOSING KEYCAP}": (
                    "name", str, None,
                    lambda given: "Your given field name is too long. Please provide another." if len(given) > 256 or len(given) <= 0 else True,
                    lambda given: given,
                ),
                "2\N{COMBINING ENCLOSING KEYCAP}": (
                    "prompt", str, None,
                    lambda given: "Your given field prompt is too short. Please provide another." if len(given) == 0 else True,
                    lambda given: given,
                ),
                "3\N{COMBINING ENCLOSING KEYCAP}": (
                    "optional", str, "Do you want this field to be optional? Type **yes** or **no**.",
                    lambda given: "You need to say either **yes** or **no** for this field." if given.lower() not in ['yes', 'no', 'true', 'false'] else True,
                    lambda given: {'yes': True, 'no': False, 'true': True, 'false': False}[given.lower()],
                ),
                "4\N{COMBINING ENCLOSING KEYCAP}": (
                    "field_type", str, "What type do you want this field to have? Type **text**, or **number**.",
                    lambda given: "You need to say either **text** or **number** for this field." if given.lower() not in ['text', 'number', 'numbers', 'int', 'integer', 'str', 'string'] else True,
                    lambda given: {'text': '1000-CHAR', 'number': 'INT', 'numbers': 'INT', 'int': 'INT', 'integer': 'INT', 'str': '1000-CHAR', 'string': '1000-CHAR'}[given.lower()],
                ),
                "5\N{COMBINING ENCLOSING KEYCAP}": None,
                self.CROSS_EMOJI: None,
            }
            if emoji == self.CROSS_EMOJI:
                raise ValueError()  # Cancel
            attr, value_converter, prompt, value_check, post_conversion_fixer = available_reactions[emoji]
        except ValueError:
            self.bot.loop.create_task(self.purge_message_list(ctx.channel, messages_to_delete))
            return False
        except TypeError:
            attr, value_converter, prompt, value_check = None, None, None, None  # Delete field

        # Get the value they asked for
        field_value_message = None
        if attr:

            # Loop so we can deal with invalid values
            while True:

                # Send the prompt
                if prompt is None:
                    prompt = f"What do you want to set the {attr} to?"
                v = await ctx.send(prompt)
                messages_to_delete.append(v)

                # Ask the user for some content
                try:
                    check = lambda m: m.author.id == ctx.author.id and m.channel.id == ctx.channel.id
                    field_value_message = await self.bot.wait_for("message", check=check, timeout=120)
                    messages_to_delete.append(field_value_message)
                    field_value = value_converter(field_value_message.content)

                # Value failed to convert
                except ValueError:
                    v = await ctx.send("I couldn't convert your provided value properly. Please provide another.")
                    messages_to_delete.append(v)
                    continue

                # Timed out
                except asyncio.TimeoutError:
                    try:
                        await ctx.send("Timed out waiting for field value.")
                    except discord.HTTPException:
                        pass
                    return None

                # Fix up the inputs
                value_is_valid = value_check(field_value)
                if isinstance(value_is_valid, str) or isinstance(value_is_valid, bool) and value_is_valid is False:
                    v = await ctx.send(value_is_valid or "Your provided value is invalid. Please provide another.")
                    messages_to_delete.append(v)
                    continue

                # And continue
                field_value = post_conversion_fixer(field_value)
                break

        # Save the data
        async with self.bot.database() as db:
            if attr:
                await db("UPDATE field SET {0}=$2 WHERE field_id=$1".format(attr), field_to_edit.field_id, field_value)
            else:
                await db("UPDATE field SET deleted=true WHERE field_id=$1", field_to_edit.field_id)

        # And done
        self.bot.loop.create_task(self.purge_message_list(ctx.channel, messages_to_delete))
        return True

    @utils.command()
    @commands.has_guild_permissions(manage_roles=True)
    @commands.bot_has_permissions(send_messages=True, external_emojis=True, add_reactions=True)
    @commands.guild_only()
    async def deletetemplate(self, ctx:utils.Context, template:localutils.Template):
        """
        Deletes a template from your guild.
        """

        # See if they're already editing that template
        if self.template_editing_locks[ctx.guild.id].locked():
            return await ctx.send("You're already editing a template.")

        # Grab the template edit lock
        async with self.template_editing_locks[ctx.guild.id]:

            # Ask for confirmation
            delete_confirmation_message = await ctx.send("By doing this, you'll delete all of the created profiles under this template as well. Would you like to proceed?")
            valid_reactions = [self.TICK_EMOJI, self.CROSS_EMOJI]
            for e in valid_reactions:
                try:
                    await delete_confirmation_message.add_reaction(e)
                except discord.HTTPException:
                    try:
                        await delete_confirmation_message.edit(content="I'm unable to add reactions to my messages.")
                    except discord.HTTPException:
                        pass
                    return
            try:
                r = await self.bot.wait_for(
                    "raw_reaction_add", timeout=120.0,
                    check=lambda p: p.message_id == delete_confirmation_message.id and str(p.emoji) in valid_reactions and p.user_id == ctx.author.id
                )
            except asyncio.TimeoutError:
                try:
                    await ctx.send("Template delete timed out - please try again later.")
                except discord.Forbidden:
                    pass
                return

            # Check if they said no
            if str(r.emoji) == self.CROSS_EMOJI:
                return await ctx.send("Got it, cancelling template delete.")

            # Delete it from the database
            async with self.bot.database() as db:
                await db("DELETE FROM template WHERE template_id=$1", template.template_id)
            self.logger.info(f"Template '{template.name}' deleted on guild {ctx.guild.id}")
            await ctx.send(f"All relevant data for template **{template.name}** (`{template.template_id}`) has been deleted.")

    @utils.command()
    @commands.has_guild_permissions(manage_roles=True)
    @commands.bot_has_permissions(send_messages=True, manage_messages=True, external_emojis=True, add_reactions=True, embed_links=True)
    @commands.guild_only()
    async def createtemplate(self, ctx:utils.Context, template_name:str=None):
        """
        Creates a new template for your guild.
        """

        # Only allow them to make one template at once
        if self.template_editing_locks[ctx.guild.id].locked():
            return await ctx.send("You're already creating a template.")

        # See if they have too many templates already
        async with self.bot.database() as db:
            template_list = await db("SELECT template_id FROM template WHERE guild_id=$1", ctx.guild.id)
            guild_settings = await db("SELECT * FROM guild_settings WHERE guild_id=$1 OR guild_id=0 ORDER BY guild_id DESC", ctx.guild.id)
        if len(template_list) >= guild_settings[0]['max_template_count']:
            return await ctx.send(f"You already have {guild_settings[0]['max_template_count']} templates set for this server, which is the maximum number allowed.")

        # And now we start creating the template itself
        async with self.template_editing_locks[ctx.guild.id]:

            # Send the flavour text behind getting a template name
            if template_name is None:
                await ctx.send(f"What name do you want to give this template? This will be used for the set and get commands; eg if the name of your template is `test`, the commands generated will be `{ctx.prefix}settest` to set a profile, `{ctx.prefix}gettest` to get a profile, and `{ctx.prefix}deletetest` to delete a profile. A profile name is case insensitive when used in commands.")

            # Get name from the messages they send
            while True:

                # Get message
                if template_name is None:
                    try:
                        name_message = await self.bot.wait_for('message', check=lambda m: m.author == ctx.author and m.channel == ctx.channel, timeout=120)

                    # Catch timeout
                    except asyncio.TimeoutError:
                        try:
                            return await ctx.send(f"{ctx.author.mention}, your template creation has timed out after 2 minutes of inactivity.")
                        except discord.Forbidden:
                            return
                    template_name = name_message.content

                # Check name for characters
                if not self.is_valid_template_name(template_name):
                    await ctx.send("You can only use normal lettering and digits in your command name. Please run this command again to set a new one.")
                    return

                # Check name for length
                if 30 >= len(template_name) >= 1:
                    pass
                else:
                    await ctx.send("The maximum length of a profile name is 30 characters. Please give another name.")
                    continue

                # Check name is unique
                async with self.bot.database() as db:
                    template_exists = await db("SELECT * FROM template WHERE guild_id=$1 AND LOWER(name)=LOWER($2)", ctx.guild.id, template_name)
                if template_exists:
                    await ctx.send(f"This server already has a template with name **{template_name}**. Please run this command again to provide another one.")
                    return
                break

            # Get an ID for the profile
            template = localutils.Template(
                template_id=uuid.uuid4(),
                colour=0x0,
                guild_id=ctx.guild.id,
                verification_channel_id=None,
                name=template_name,
                archive_channel_id=None,
                role_id=None,
                max_profile_count=1,
                max_field_count=10,
            )

        # Save it all to database
        async with self.bot.database() as db:
            await db(
                """INSERT INTO template (template_id, name, colour, guild_id, verification_channel_id, archive_channel_id)
                VALUES ($1, $2, $3, $4, $5, $6)""",
                template.template_id, template.name, template.colour, template.guild_id, template.verification_channel_id, template.archive_channel_id
            )

        # Output to user
        self.logger.info(f"New template '{template.name}' created on guild {ctx.guild.id}")
        await ctx.invoke(self.bot.get_command("edittemplate"), template)

    async def create_new_field(self, ctx:utils.Context, template:localutils.Template, index:int, image_set:bool=False, prompt_for_creation:bool=True, delete_messages:bool=False) -> typing.Optional[localutils.Field]:
        """
        Talk a user through creating a new field for their template.
        """

        # Here are some things we can use later
        message_check = lambda m: m.author == ctx.author and m.channel == ctx.channel
        okay_reaction_check = lambda p: str(p.emoji) in prompt_emoji and p.user_id == ctx.author.id
        prompt_emoji = [self.TICK_EMOJI, self.CROSS_EMOJI]
        messages_to_delete = []

        # Ask if they want a new field
        if prompt_for_creation:
            field_message = await ctx.send("Do you want to make a new field for your profile?", embed=template.build_embed(self.bot))
            messages_to_delete.append(field_message)
            for e in prompt_emoji:
                try:
                    await field_message.add_reaction(e)
                except discord.Forbidden:
                    try:
                        await field_message.delete()
                    except discord.NotFound:
                        pass
                    await ctx.send("I tried to add a reaction to my message, but I was unable to. Please update my permissions for this channel and try again.")
                    return None

            # Here's us waiting for the "do you want to make a new field" reaction
            try:
                reaction = await self.bot.wait_for('raw_reaction_add', check=okay_reaction_check, timeout=120)
            except asyncio.TimeoutError:
                try:
                    await ctx.send("Creating a new field has timed out. The profile is being created with the fields currently added.")
                except (discord.Forbidden, discord.NotFound):
                    pass
                return None

            # See if they don't wanna continue
            if str(reaction.emoji) == self.CROSS_EMOJI:
                return None
            await field_message.edit(content=field_message.content, embed=None)

        # Get a name for the new field
        v = await ctx.send("What name should this field have? This is the name shown on the embed, so it should be something like 'Name', 'Age', 'Gender', etc.")
        messages_to_delete.append(v)
        while True:
            try:
                field_name_message = await self.bot.wait_for('message', check=message_check, timeout=120)
                messages_to_delete.append(field_name_message)
            except asyncio.TimeoutError:
                try:
                    await ctx.send("Creating a new field has timed out. The profile is being created with the fields currently added.")
                except (discord.Forbidden, discord.NotFound):
                    pass
                return None

            # Check if if name is too long
            if 256 >= len(field_name_message.content) >= 1:
                break
            else:
                v = await ctx.send("The maximum length of a field name is 256 characters. Please provide another name.")
                messages_to_delete.append(v)
        field_name = field_name_message.content

        # Get a prompt for the field
        v = await ctx.send("What message should I send when I'm asking people to fill out this field? This should be a question or prompt, eg 'What is your name/age/gender/etc'.")
        messages_to_delete.append(v)
        while True:
            try:
                field_prompt_message = await self.bot.wait_for('message', check=message_check, timeout=120)
                messages_to_delete.append(field_prompt_message)
            except asyncio.TimeoutError:
                try:
                    await ctx.send("Creating a new field has timed out. The profile is being created with the fields currently added.")
                except (discord.Forbidden, discord.NotFound):
                    pass
                return None

            if len(field_prompt_message.content) >= 1:
                break
            else:
                v = await ctx.send("You need to actually give text for the prompt :/")
                messages_to_delete.append(v)
        field_prompt = field_prompt_message.content
        prompt_is_command = bool(localutils.CommandProcessor.COMMAND_REGEX.search(field_prompt))

        # If it's a command, then we don't need to deal with this
        if not prompt_is_command:

            # Get field optional
            prompt_message = await ctx.send("Is this field optional?")
            messages_to_delete.append(prompt_message)
            for e in prompt_emoji:
                await prompt_message.add_reaction(e)
            try:
                field_optional_reaction = await self.bot.wait_for('raw_reaction_add', check=okay_reaction_check, timeout=120)
                field_optional_emoji = str(field_optional_reaction.emoji)
            except asyncio.TimeoutError:
                field_optional_emoji = self.CROSS_EMOJI
            field_optional = field_optional_emoji == self.TICK_EMOJI

            # Get timeout
            v = await ctx.send("How many seconds should I wait for people to fill out this field (I recommend 120 - that's 2 minutes)? The minimum is 30, and the maximum is 600.")
            messages_to_delete.append(v)
            while True:
                try:
                    field_timeout_message = await self.bot.wait_for('message', check=message_check, timeout=120)
                    messages_to_delete.append(field_timeout_message)
                except asyncio.TimeoutError:
                    await ctx.send("Creating a new field has timed out. The profile is being created with the fields currently added.")
                    return None
                try:
                    timeout = int(field_timeout_message.content)
                    if timeout < 30:
                        raise ValueError()
                    break
                except ValueError:
                    v = await ctx.send("I couldn't convert your message into a valid number - the minimum is 30 seconds. Please try again.")
                    messages_to_delete.append(v)
            field_timeout = min([timeout, 600])

            # Ask for field type
            if image_set:
                text = f"What type is this field? Will you be getting numbers ({self.NUMBERS_EMOJI}), or any text ({self.LETTERS_EMOJI})?"
            else:
                text = f"What type is this field? Will you be getting numbers ({self.NUMBERS_EMOJI}), any text ({self.LETTERS_EMOJI}), or an image ({self.PICTURE_EMOJI})?"
            field_type_message = await ctx.send(text)
            messages_to_delete.append(field_type_message)

            # Add reactions
            await field_type_message.add_reaction(self.NUMBERS_EMOJI)
            await field_type_message.add_reaction(self.LETTERS_EMOJI)
            if not image_set:
                await field_type_message.add_reaction(self.PICTURE_EMOJI)

            # See what they said
            field_type_emoji = [self.NUMBERS_EMOJI, self.LETTERS_EMOJI, self.PICTURE_EMOJI]  # self.TICK_EMOJI
            field_type_check = lambda p: str(p.emoji) in field_type_emoji and p.user_id == ctx.author.id
            try:
                reaction = await self.bot.wait_for('raw_reaction_add', check=field_type_check, timeout=120)
                emoji = str(reaction.emoji)
            except asyncio.TimeoutError:
                try:
                    await ctx.send("Picking a field type has timed out - defaulting to text.")
                except (discord.Forbidden, discord.NotFound):
                    pass
                emoji = self.LETTERS_EMOJI

            # Change that emoji into a datatype
            field_type = {
                self.NUMBERS_EMOJI: localutils.NumberField,
                self.LETTERS_EMOJI: localutils.TextField,
                self.PICTURE_EMOJI: localutils.ImageField,
            }[emoji]
            if isinstance(field_type, localutils.ImageField) and image_set:
                raise Exception("You shouldn't be able to set two image fields.")

        # Set some defaults for the field stuff
        else:
            field_optional = False
            field_timeout = 15
            field_type = localutils.TextField

        # Make the field object
        field = localutils.Field(
            field_id=uuid.uuid4(),
            name=field_name,
            index=index,
            prompt=field_prompt,
            timeout=field_timeout,
            field_type=field_type,
            template_id=template.template_id,
            optional=field_optional,
            deleted=False,
        )

        # See if we need to delete things
        if delete_messages:
            self.bot.loop.create_task(self.purge_message_list(ctx.channel, messages_to_delete))

        # And we done
        return field


def setup(bot:utils.Bot):
    x = ProfileTemplates(bot)
    bot.add_cog(x)
