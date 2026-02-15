import asyncio
from database import engine, get_db
from models import Entity, EntityType, Project, Stage, ApprovalStatus, EntityType
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select

async def init_demo():
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as session:
        # Add Antigravity if not exists
        result = await session.execute(select(Entity).filter(Entity.name == 'Antigravity'))
        ag = result.scalar_one_or_none()
        if not ag:
            ag = Entity(
                name='Antigravity', 
                entity_type=EntityType.AGENT, 
                skills='AI, Management, Heavy Lifting',
                is_active=True
            )
            session.add(ag)
            await session.commit()
            await session.refresh(ag)
            print("Antigravity added")
        else:
            print("Antigravity already exists")
            ag.is_active = True
            await session.commit()

        # Add a demo project managed by Antigravity if not exists
        result = await session.execute(select(Project).filter(Project.name == 'Heavy Lifting Project'))
        project = result.scalar_one_or_none()
        if not project:
            project = Project(
                name='Heavy Lifting Project',
                description='Demonstrating Antigravity manager features and heavy lifting mode.',
                creator_id=ag.id,
                approval_status=ApprovalStatus.APPROVED
            )
            session.add(project)
            await session.commit()
            await session.refresh(project)
            
            # Add stages
            stages = [
                Stage(name="Backlog", order=1, project_id=project.id),
                Stage(name="In Progress", order=2, project_id=project.id),
                Stage(name="Done", order=3, project_id=project.id)
            ]
            for s in stages: session.add(s)
            await session.commit()
            print("Demo project added")
        else:
            print("Demo project already exists")

if __name__ == "__main__":
    asyncio.run(init_demo())
